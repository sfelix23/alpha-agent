"""Tests del gateway LLM dentro de claude_analyst.

Foco:
- Cache hit no invoca providers.
- Budget agotado fuerza fallback.
- 400/401/403 deshabilita provider 24h sin retry.
- 429/5xx reintenta con backoff y cae al siguiente si agota.
- Rate limit local bloquea y pasa al siguiente.
- Cascada completa: si todos fallan → None.
"""

from __future__ import annotations

import pytest

from alpha_agent.config import LLM
from alpha_agent.news import claude_analyst as ca
from alpha_agent.news.claude_analyst import (
    ProviderDisabled,
    ProviderError,
    ProviderResult,
)


# ── Fakes ────────────────────────────────────────────────────────────────────


def make_fake_call(
    provider: str,
    *,
    text: str = "ok",
    raises: Exception | None = None,
    raises_n_times: int = 0,
):
    """Construye una función `_call_<provider>(prompt, max_tokens, alias)` fake."""
    counter = {"n": 0}

    def _fake(prompt, max_tokens, alias):
        counter["n"] += 1
        if raises and counter["n"] <= raises_n_times:
            raise raises
        return ProviderResult(
            text=text, provider=provider, model=f"{provider}-test-model",
            tokens_in=10, tokens_out=5, cost_usd=0.0, latency_ms=50,
        )

    return _fake, counter


@pytest.fixture
def install_providers(monkeypatch):
    """Reemplaza _PROVIDERS y _AVAILABILITY con fakes."""

    def _install(**fakes):
        # fakes: provider_id → (call_fn, counter)
        new_providers = {pid: fn for pid, (fn, _) in fakes.items()}
        new_availability = {pid: (lambda: True) for pid in fakes}
        monkeypatch.setattr(ca, "_PROVIDERS", new_providers)
        monkeypatch.setattr(ca, "_AVAILABILITY", new_availability)
        return {pid: counter for pid, (_, counter) in fakes.items()}

    return _install


@pytest.fixture
def set_cascade(monkeypatch):
    def _set(purpose, *items):
        from alpha_agent import config
        new_cascade = dict(config.LLM_CASCADE_BY_PURPOSE)
        new_cascade[purpose] = list(items)
        monkeypatch.setattr(config, "LLM_CASCADE_BY_PURPOSE", new_cascade)
        monkeypatch.setattr(ca, "LLM_CASCADE_BY_PURPOSE", new_cascade)

    return _set


# ── Tests del gateway ────────────────────────────────────────────────────────


def test_cache_hit_no_provider_call(install_providers, set_cascade):
    fn, counter = make_fake_call("groq", text="from-network")
    counters = install_providers(groq=(fn, counter))
    set_cascade("event_score", ("groq", "fast"))

    model = ca._resolve_model_name("groq", "fast")
    key = ca.cache_key("hello", model, 10, "")
    ca.cache_put(
        key, purpose="event_score", provider="groq", model=model,
        response="from-cache", tokens_in=1, tokens_out=1,
    )

    out = ca.call_llm("hello", purpose="event_score", max_tokens=10)
    assert out == "from-cache"
    assert counters["groq"]["n"] == 0


def test_basic_call_writes_cache_and_budget(install_providers, set_cascade):
    fn, counter = make_fake_call("groq", text="bullish")
    counters = install_providers(groq=(fn, counter))
    set_cascade("event_score", ("groq", "fast"))

    assert ca.call_llm("classify", purpose="event_score", max_tokens=10) == "bullish"
    assert counters["groq"]["n"] == 1

    # Segunda llamada → cache hit, no se invoca de nuevo.
    assert ca.call_llm("classify", purpose="event_score", max_tokens=10) == "bullish"
    assert counters["groq"]["n"] == 1

    snap = ca.get_gateway_status()
    assert snap["providers"]["groq"]["calls"] == 2  # 1 real + 1 cache hit
    assert snap["providers"]["groq"]["cache_hits"] == 1


def test_provider_disabled_falls_through(install_providers, set_cascade):
    fn_groq, c_groq = make_fake_call("groq", raises=ProviderDisabled("flag OFF"), raises_n_times=10)
    fn_gem, c_gem = make_fake_call("gemini", text="from-gemini")
    install_providers(groq=(fn_groq, c_groq), gemini=(fn_gem, c_gem))
    set_cascade("event_score", ("groq", "fast"), ("gemini", "default"))

    assert ca.call_llm("hi", purpose="event_score", max_tokens=10) == "from-gemini"
    assert c_groq["n"] == 1  # NO retry en disabled


def test_transient_error_retries_then_falls_through(install_providers, set_cascade, monkeypatch):
    monkeypatch.setattr(LLM, "retry_backoff_seconds", (0.0, 0.0))
    fn_groq, c_groq = make_fake_call("groq", raises=ProviderError("5xx"), raises_n_times=99)
    fn_gem, c_gem = make_fake_call("gemini", text="from-gemini")
    install_providers(groq=(fn_groq, c_groq), gemini=(fn_gem, c_gem))
    set_cascade("event_score", ("groq", "fast"), ("gemini", "default"))

    assert ca.call_llm("hi", purpose="event_score", max_tokens=10) == "from-gemini"
    # 1 intento inicial + retry_max_attempts retries.
    assert c_groq["n"] == LLM.retry_max_attempts + 1


def test_400_disables_provider_no_retry(monkeypatch):
    """Un 400 levantado por el SDK debe auto-disable el provider 24h sin retry."""
    # Iter3: enable_anthropic ahora es @property que lee env var ENABLE_ANTHROPIC.
    # Para activarlo en el test, seteamos el env var.
    monkeypatch.setenv("ENABLE_ANTHROPIC", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    class FakeSDKError(Exception):
        status_code = 400

        def __str__(self):
            return "empresa deshabilitada"

    class FakeClient:
        class messages:
            @staticmethod
            def create(**kw):
                raise FakeSDKError()

    monkeypatch.setattr(ca, "_clients", {"anthropic": FakeClient()})

    with pytest.raises(ProviderDisabled):
        ca._call_anthropic("x", 10, "fast")

    disabled, reason = ca.is_provider_disabled("anthropic")
    assert disabled is True
    assert "400" in (reason or "")


def test_rate_limit_local_skips_to_next(install_providers, set_cascade, monkeypatch):
    fn_groq, c_groq = make_fake_call("groq", text="should-not-fire")
    fn_gem, c_gem = make_fake_call("gemini", text="from-gemini")
    install_providers(groq=(fn_groq, c_groq), gemini=(fn_gem, c_gem))
    set_cascade("event_score", ("groq", "fast"), ("gemini", "default"))

    monkeypatch.setattr(ca, "rate_acquire", lambda p: p != "groq")

    assert ca.call_llm("hi", purpose="event_score", max_tokens=10) == "from-gemini"
    assert c_groq["n"] == 0


def test_budget_exhausted_skips_provider(install_providers, set_cascade, monkeypatch):
    fn_a, c_a = make_fake_call("anthropic", text="from-anthropic")
    fn_g, c_g = make_fake_call("groq", text="from-groq")
    install_providers(anthropic=(fn_a, c_a), groq=(fn_g, c_g))
    set_cascade("assess_position", ("anthropic", "fast"), ("groq", "fast"))

    monkeypatch.setattr(ca, "is_budget_exhausted", lambda p: p == "anthropic")

    assert ca.call_llm("hi", purpose="assess_position", max_tokens=10) == "from-groq"
    assert c_a["n"] == 0


def test_all_providers_fail_returns_none(install_providers, set_cascade, monkeypatch):
    monkeypatch.setattr(LLM, "retry_backoff_seconds", (0.0, 0.0))
    fn_g, c_g = make_fake_call("groq", raises=ProviderDisabled("flag OFF"), raises_n_times=10)
    fn_gem, c_gem = make_fake_call("gemini", raises=ProviderError("oops"), raises_n_times=10)
    install_providers(groq=(fn_g, c_g), gemini=(fn_gem, c_gem))
    set_cascade("event_score", ("groq", "fast"), ("gemini", "default"))

    assert ca.call_llm("hi", purpose="event_score", max_tokens=10) is None


# ── Tests de budget ──────────────────────────────────────────────────────────


def test_budget_records_call():
    ca.record_call("groq", tokens_in=100, tokens_out=50, cost_usd=0.001)
    ca.record_call("groq", tokens_in=200, tokens_out=100, cost_usd=0.002)

    snap = ca.get_gateway_status()
    assert snap["providers"]["groq"]["calls"] == 2
    assert snap["providers"]["groq"]["tokens_in"] == 300
    assert snap["providers"]["groq"]["tokens_out"] == 150
    assert snap["providers"]["groq"]["cost_usd"] == pytest.approx(0.003, abs=1e-6)


def test_provider_disable_and_re_enable():
    ca.disable_provider("anthropic", hours=24.0, reason="HTTP 400 test")
    disabled, reason = ca.is_provider_disabled("anthropic")
    assert disabled is True
    assert "400" in (reason or "")

    ca.enable_provider("anthropic")
    disabled, _ = ca.is_provider_disabled("anthropic")
    assert disabled is False


def test_disable_expires():
    ca.disable_provider("anthropic", hours=0.0001, reason="short test")
    import time as _time
    _time.sleep(0.5)
    disabled, _ = ca.is_provider_disabled("anthropic")
    assert disabled is False


# ── Tests del cache ──────────────────────────────────────────────────────────


def test_cache_put_and_get_within_ttl():
    key = ca.cache_key("p", "m", 10, "")
    ca.cache_put(key, purpose="event_score", provider="groq", model="m", response="x", tokens_in=1, tokens_out=1)
    hit = ca.cache_get(key, purpose="event_score")
    assert hit is not None
    text, provider, _ = hit
    assert text == "x"
    assert provider == "groq"


def test_cache_miss_after_ttl(monkeypatch):
    monkeypatch.setattr(LLM, "cache_ttl_event_score_h", 0.0)
    key = ca.cache_key("p", "m", 10, "")
    ca.cache_put(key, purpose="event_score", provider="groq", model="m", response="x", tokens_in=1, tokens_out=1)
    assert ca.cache_get(key, purpose="event_score") is None


# ── Tests del rate limiter ───────────────────────────────────────────────────


def test_rate_limit_token_bucket(monkeypatch):
    monkeypatch.setattr(LLM, "rate_limit_groq_per_min", 3)
    ca.rate_reset()
    assert ca.rate_acquire("groq") is True
    assert ca.rate_acquire("groq") is True
    assert ca.rate_acquire("groq") is True
    assert ca.rate_acquire("groq") is False  # bucket lleno
    assert ca.rate_remaining("groq") == 0


def test_rate_limit_independent_per_provider():
    ca.rate_reset()
    for _ in range(50):
        ca.rate_acquire("groq")
    assert ca.rate_acquire("gemini") is True
