"""Tests del gateway LLM — cubre los casos críticos sin tocar red.

Foco:
- Cache hit no invoca providers.
- Budget agotado fuerza fallback.
- 400/401/403 deshabilita provider 24h sin retry.
- 429/5xx reintenta con backoff y cae al siguiente si agota.
- Rate limit local bloquea y pasa al siguiente.
- Cascada completa: si todos fallan → None.
- `record_call` actualiza JSON correctamente.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

from alpha_agent.config import LLM
from alpha_agent.llm import budget, cache, gateway, rate_limit
from alpha_agent.llm.providers import ProviderDisabled, ProviderError, ProviderResult


# ── Fakes ─────────────────────────────────────────────────────────────────────


class FakeProvider:
    """Stand-in para BaseProvider en los tests del gateway."""

    def __init__(
        self,
        name: str,
        *,
        available: bool = True,
        text: str = "ok",
        raises: Exception | None = None,
        raises_n_times: int = 0,
        tokens_in: int = 10,
        tokens_out: int = 5,
        cost_usd: float = 0.0,
        latency_ms: int = 50,
    ) -> None:
        self.name = name
        self._available = available
        self._text = text
        self._raises = raises
        self._raises_n_times = raises_n_times
        self._call_count = 0
        self._tokens_in = tokens_in
        self._tokens_out = tokens_out
        self._cost = cost_usd
        self._latency = latency_ms

    def is_available(self) -> bool:
        return self._available

    def call(self, prompt: str, *, max_tokens: int, model_alias: str = "fast") -> ProviderResult:
        self._call_count += 1
        if self._raises and self._call_count <= self._raises_n_times:
            raise self._raises
        return ProviderResult(
            text=self._text,
            provider=self.name,
            model=f"{self.name}-test-model",
            tokens_in=self._tokens_in,
            tokens_out=self._tokens_out,
            cost_usd=self._cost,
            latency_ms=self._latency,
        )


@pytest.fixture
def install_fakes(monkeypatch):
    """Helper para reemplazar el registro de providers del gateway."""

    def _install(**fakes: FakeProvider) -> dict[str, FakeProvider]:
        monkeypatch.setattr(gateway, "_PROVIDERS", dict(fakes))
        return fakes

    return _install


@pytest.fixture
def single_provider_cascade(monkeypatch):
    """Reduce la cascada de un purpose a un sólo provider para aislar."""

    def _set(purpose: str, *items: tuple[str, str]) -> None:
        from alpha_agent import config
        new_cascade = dict(config.LLM_CASCADE_BY_PURPOSE)
        new_cascade[purpose] = list(items)
        monkeypatch.setattr(config, "LLM_CASCADE_BY_PURPOSE", new_cascade)
        monkeypatch.setattr(gateway, "LLM_CASCADE_BY_PURPOSE", new_cascade)

    return _set


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_cache_hit_no_provider_call(install_fakes, single_provider_cascade):
    """Si el cache tiene la respuesta, ningún provider se invoca."""
    fakes = install_fakes(groq=FakeProvider("groq", text="from-network"))
    single_provider_cascade("event_score", ("groq", "fast"))

    # Pre-poblar el cache con la key exacta que va a generar el gateway.
    model = gateway._resolve_model_name("groq", "fast")
    key = cache.cache_key("hello", model, 10, "")
    cache.put(
        key,
        purpose="event_score",
        provider="groq",
        model=model,
        response="from-cache",
        tokens_in=1,
        tokens_out=1,
    )

    out = gateway.call_llm("hello", purpose="event_score", max_tokens=10)
    assert out == "from-cache"
    assert fakes["groq"]._call_count == 0


def test_basic_call_writes_cache_and_budget(install_fakes, single_provider_cascade):
    fakes = install_fakes(groq=FakeProvider("groq", text="bullish", cost_usd=0.0))
    single_provider_cascade("event_score", ("groq", "fast"))

    out = gateway.call_llm("classify", purpose="event_score", max_tokens=10)
    assert out == "bullish"
    assert fakes["groq"]._call_count == 1

    # Segunda llamada con el mismo prompt → cache hit, no se invoca de nuevo.
    out2 = gateway.call_llm("classify", purpose="event_score", max_tokens=10)
    assert out2 == "bullish"
    assert fakes["groq"]._call_count == 1

    snap = budget.get_snapshot()
    assert snap["providers"]["groq"]["calls"] == 2  # 1 real + 1 cache hit
    assert snap["providers"]["groq"]["cache_hits"] == 1


def test_provider_disabled_falls_through(install_fakes, single_provider_cascade):
    """ProviderDisabled del primero → pasa al segundo sin retry."""
    fakes = install_fakes(
        groq=FakeProvider("groq", raises=ProviderDisabled("flag OFF"), raises_n_times=10),
        gemini=FakeProvider("gemini", text="from-gemini"),
    )
    single_provider_cascade("event_score", ("groq", "fast"), ("gemini", "default"))

    out = gateway.call_llm("hi", purpose="event_score", max_tokens=10)
    assert out == "from-gemini"
    # Groq se invocó una sola vez (no se reintenta).
    assert fakes["groq"]._call_count == 1


def test_transient_error_retries_then_falls_through(install_fakes, single_provider_cascade, monkeypatch):
    """ProviderError transitorio → agota retries y pasa al siguiente."""
    # Acelerar backoff a 0s para que el test no demore.
    monkeypatch.setattr(LLM, "retry_backoff_seconds", (0.0, 0.0))

    flaky = FakeProvider(
        "groq",
        raises=ProviderError("5xx transient"),
        raises_n_times=99,
    )
    fakes = install_fakes(
        groq=flaky,
        gemini=FakeProvider("gemini", text="from-gemini"),
    )
    single_provider_cascade("event_score", ("groq", "fast"), ("gemini", "default"))

    out = gateway.call_llm("hi", purpose="event_score", max_tokens=10)
    assert out == "from-gemini"
    # 1 intento + retry_max_attempts retries = total intentos.
    assert flaky._call_count == LLM.retry_max_attempts + 1


def test_400_disables_provider_no_retry(install_fakes, single_provider_cascade):
    """Un 400/401/403 NO debe reintentarse y debe auto-disable el provider."""
    # Anthropic provider real (no fake) atrapa el 400 vía _extract_status_code.
    from alpha_agent.llm.providers.anthropic_provider import AnthropicProvider

    class FakeAnthropicSDKError(Exception):
        status_code = 400

        def __str__(self):
            return "empresa deshabilitada"

    prov = AnthropicProvider()

    # Forzar que is_available devuelva True saltando los flags.
    monkeypatched = type("P", (AnthropicProvider,), {"is_available": lambda self: True})()

    # Inyectar un cliente fake que lance el error.
    class FakeClient:
        class messages:
            @staticmethod
            def create(**kw):
                raise FakeAnthropicSDKError()

    monkeypatched._client = FakeClient()

    with pytest.raises(ProviderDisabled):
        monkeypatched.call("x", max_tokens=10, model_alias="fast")

    # Tras el 400, el state JSON marca a anthropic como disabled.
    disabled, reason = budget.is_provider_disabled("anthropic")
    assert disabled is True
    assert "400" in (reason or "")


def test_rate_limit_local_skips_to_next(install_fakes, single_provider_cascade, monkeypatch):
    """Si el rate limiter local rechaza, el gateway pasa al siguiente sin invocar."""
    fakes = install_fakes(
        groq=FakeProvider("groq", text="should-not-fire"),
        gemini=FakeProvider("gemini", text="from-gemini"),
    )
    single_provider_cascade("event_score", ("groq", "fast"), ("gemini", "default"))

    # Forzar que try_acquire("groq") devuelva False.
    monkeypatch.setattr(rate_limit, "try_acquire", lambda p: p != "groq")

    out = gateway.call_llm("hi", purpose="event_score", max_tokens=10)
    assert out == "from-gemini"
    assert fakes["groq"]._call_count == 0


def test_budget_exhausted_skips_provider(install_fakes, single_provider_cascade, monkeypatch):
    """Si el budget está agotado para un provider, se salta."""
    fakes = install_fakes(
        anthropic=FakeProvider("anthropic", text="from-anthropic"),
        groq=FakeProvider("groq", text="from-groq"),
    )
    single_provider_cascade("assess_position", ("anthropic", "fast"), ("groq", "fast"))

    monkeypatch.setattr(budget, "is_budget_exhausted", lambda p: p == "anthropic")

    out = gateway.call_llm("hi", purpose="assess_position", max_tokens=10)
    assert out == "from-groq"
    assert fakes["anthropic"]._call_count == 0


def test_all_providers_fail_returns_none(install_fakes, single_provider_cascade, monkeypatch):
    monkeypatch.setattr(LLM, "retry_backoff_seconds", (0.0, 0.0))
    fakes = install_fakes(
        groq=FakeProvider("groq", raises=ProviderDisabled("flag OFF"), raises_n_times=10),
        gemini=FakeProvider("gemini", raises=ProviderError("oops"), raises_n_times=10),
    )
    single_provider_cascade("event_score", ("groq", "fast"), ("gemini", "default"))

    out = gateway.call_llm("hi", purpose="event_score", max_tokens=10)
    assert out is None


# ── Tests de budget.py ────────────────────────────────────────────────────────


def test_budget_records_call_and_resets_daily():
    budget.record_call("groq", tokens_in=100, tokens_out=50, cost_usd=0.001)
    budget.record_call("groq", tokens_in=200, tokens_out=100, cost_usd=0.002)

    snap = budget.get_snapshot()
    assert snap["providers"]["groq"]["calls"] == 2
    assert snap["providers"]["groq"]["tokens_in"] == 300
    assert snap["providers"]["groq"]["tokens_out"] == 150
    assert snap["providers"]["groq"]["cost_usd"] == pytest.approx(0.003, abs=1e-6)


def test_provider_disable_and_re_enable():
    budget.disable_provider("anthropic", hours=24.0, reason="HTTP 400 test")
    disabled, reason = budget.is_provider_disabled("anthropic")
    assert disabled is True
    assert "400" in (reason or "")

    budget.enable_provider("anthropic")
    disabled, _ = budget.is_provider_disabled("anthropic")
    assert disabled is False


def test_disable_expires(monkeypatch):
    """Si el `until` ya pasó, is_provider_disabled lo limpia y devuelve False."""
    budget.disable_provider("anthropic", hours=0.0001, reason="short test")
    import time as _time
    _time.sleep(0.5)
    disabled, _ = budget.is_provider_disabled("anthropic")
    assert disabled is False


# ── Tests de cache.py ─────────────────────────────────────────────────────────


def test_cache_put_and_get_within_ttl():
    key = cache.cache_key("p", "m", 10, "")
    cache.put(key, purpose="event_score", provider="groq", model="m", response="x", tokens_in=1, tokens_out=1)
    hit = cache.get(key, purpose="event_score")
    assert hit is not None
    text, provider, model = hit
    assert text == "x"
    assert provider == "groq"


def test_cache_miss_after_ttl(monkeypatch):
    """Forzar TTL=0h → la entrada se considera expirada inmediatamente."""
    monkeypatch.setattr(LLM, "cache_ttl_event_score_h", 0.0)
    key = cache.cache_key("p", "m", 10, "")
    cache.put(key, purpose="event_score", provider="groq", model="m", response="x", tokens_in=1, tokens_out=1)
    hit = cache.get(key, purpose="event_score")
    assert hit is None


# ── Tests de rate_limit.py ────────────────────────────────────────────────────


def test_rate_limit_token_bucket(monkeypatch):
    monkeypatch.setattr(LLM, "rate_limit_groq_per_min", 3)
    rate_limit.reset()
    assert rate_limit.try_acquire("groq") is True
    assert rate_limit.try_acquire("groq") is True
    assert rate_limit.try_acquire("groq") is True
    assert rate_limit.try_acquire("groq") is False  # bucket lleno
    assert rate_limit.get_remaining("groq") == 0


def test_rate_limit_independent_per_provider():
    rate_limit.reset()
    for _ in range(50):
        rate_limit.try_acquire("groq")
    # Gemini sigue limpio.
    assert rate_limit.try_acquire("gemini") is True
