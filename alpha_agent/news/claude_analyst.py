"""LLM analyst — multi-provider con budget, cache, rate limit y fallback.

A pesar del nombre histórico, este módulo NO está atado a Anthropic. Es el
punto único de entrada para todas las llamadas LLM del sistema. Maneja
una cascada de providers (Groq → Gemini → DeepSeek → OpenRouter → Anthropic)
con caching, presupuesto diario y rate limiting local.

Funciones públicas legacy (las mismas que antes — el resto del código las
sigue llamando igual):
  - assess_position()        → CLOSE/HOLD/REDUCE para una posición abierta
  - build_macro_narrative()  → 2 frases macro para el WhatsApp brief
  - wall_street_analysis()   → tesis profunda de inversión
  - risk_debate()            → debate bull/bear pre-ejecución
  - score_event_impact()     → clasificación de impacto de un headline

Funciones públicas nuevas (Sesión 1 del plan):
  - call_llm()               → entry point unificado con cascada y cache
  - get_gateway_status()     → snapshot para dashboard / Telegram

Política Anthropic (causa del 400 "empresa deshabilitada"):
  - LLM.enable_anthropic default OFF — encender manual cuando haya créditos
  - 400/401/403 deshabilitan el provider 24h SIN retry (no amplificar abuse-flag)
  - Sonnet detrás de flag separado (más caro)

Persistencia:
  - signals/llm_budget.json        — calls, tokens, costo USD por provider (reset diario UTC)
  - signals/llm_provider_state.json — providers auto-deshabilitados con TTL
  - signals/llm_cache.sqlite       — respuestas cacheadas con TTL por purpose
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from alpha_agent.config import LLM, LLM_CASCADE_BY_PURPOSE, PATHS

logger = logging.getLogger(__name__)

Purpose = Literal[
    "sentiment",
    "event_score",
    "assess_position",
    "narrative",
    "wall_street",
    "risk_debate",
]


# ── Persistence paths ────────────────────────────────────────────────────────

_BUDGET_PATH = PATHS.signals_dir / "llm_budget.json"
_STATE_PATH = PATHS.signals_dir / "llm_provider_state.json"
_CACHE_DB_PATH = PATHS.signals_dir / "llm_cache.sqlite"


# ── Errors & types ───────────────────────────────────────────────────────────


class ProviderError(Exception):
    """Error de provider que el gateway considera transitorio (5xx, timeout, parsing)."""


class ProviderDisabled(ProviderError):
    """Provider apagado por flag/key/auto-disable. NO se reintenta."""


class RateLimitExceeded(ProviderError):
    """Token bucket local saturado. El gateway pasa al siguiente provider."""


@dataclass(frozen=True)
class ProviderResult:
    text: str
    provider: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: int


# ── Helpers de I/O ───────────────────────────────────────────────────────────


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return dict(default)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("llm: archivo corrupto %s (%s) — reset.", path.name, e)
        return dict(default)


def _atomic_write(path: Path, data: dict) -> None:
    """Escribe via tempfile + replace para evitar corrupción si el proceso muere."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _extract_status_code(exc: Exception) -> int | None:
    """Best-effort extraction de status code de un error de SDK (any provider)."""
    for attr in ("status_code", "status", "code"):
        v = getattr(exc, attr, None)
        if isinstance(v, int):
            return v
    resp = getattr(exc, "response", None)
    if resp is not None:
        v = getattr(resp, "status_code", None)
        if isinstance(v, int):
            return v
    return None


# ── Budget tracking ──────────────────────────────────────────────────────────


def _load_budget() -> dict:
    data = _load_json(_BUDGET_PATH, {"date": _today_utc(), "providers": {}})
    if data.get("date") != _today_utc():
        # Día nuevo — archivar el anterior en logs y resetear.
        logger.info("llm budget: reset diario (era %s, ahora %s)", data.get("date"), _today_utc())
        archive = PATHS.logs_dir / f"llm_usage_{data.get('date', 'unknown')}.json"
        try:
            _atomic_write(archive, data)
        except OSError as e:
            logger.warning("llm budget: no se pudo archivar %s (%s)", archive.name, e)
        data = {"date": _today_utc(), "providers": {}}
        _atomic_write(_BUDGET_PATH, data)
    return data


def record_call(
    provider: str,
    *,
    tokens_in: int,
    tokens_out: int,
    cost_usd: float,
    cache_hit: bool = False,
    fallback_from: str | None = None,
) -> None:
    """Registra una llamada en el budget del día."""
    data = _load_budget()
    p = data["providers"].setdefault(
        provider,
        {"calls": 0, "cache_hits": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "fallbacks_in": 0},
    )
    p["calls"] += 1
    if cache_hit:
        p["cache_hits"] += 1
    else:
        p["tokens_in"] += int(tokens_in)
        p["tokens_out"] += int(tokens_out)
        p["cost_usd"] = round(p["cost_usd"] + float(cost_usd), 6)
    if fallback_from:
        p["fallbacks_in"] += 1
    _atomic_write(_BUDGET_PATH, data)


def get_today_cost(provider: str | None = None) -> float:
    """Costo USD acumulado hoy. Si provider es None, suma todos."""
    data = _load_budget()
    if provider:
        return float(data["providers"].get(provider, {}).get("cost_usd", 0.0))
    return sum(p.get("cost_usd", 0.0) for p in data["providers"].values())


def is_budget_exhausted(provider: str) -> bool:
    """¿Se acabó el presupuesto para este provider (o el total)?"""
    if provider == "anthropic" and get_today_cost("anthropic") >= LLM.daily_anthropic_budget_usd:
        return True
    return get_today_cost(None) >= LLM.daily_total_budget_usd


def _load_state() -> dict:
    return _load_json(_STATE_PATH, {"disabled": {}})


def disable_provider(provider: str, hours: float, reason: str) -> None:
    """Marca al provider como deshabilitado hasta `now + hours`.

    Se llama tras un 400/401/403 — error estructural que no se arregla con retry.
    """
    state = _load_state()
    until = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
    state["disabled"][provider] = {"until": until, "reason": reason}
    _atomic_write(_STATE_PATH, state)
    logger.warning("llm: provider '%s' deshabilitado hasta %s — %s", provider, until, reason)


def is_provider_disabled(provider: str) -> tuple[bool, str | None]:
    state = _load_state()
    entry = state["disabled"].get(provider)
    if not entry:
        return False, None
    try:
        until = datetime.fromisoformat(entry["until"])
    except (ValueError, TypeError):
        return False, None
    if datetime.now(timezone.utc) >= until:
        state["disabled"].pop(provider, None)
        _atomic_write(_STATE_PATH, state)
        return False, None
    return True, entry.get("reason")


def enable_provider(provider: str) -> None:
    """Re-habilita un provider manualmente (saca el auto-disable)."""
    state = _load_state()
    if state["disabled"].pop(provider, None) is not None:
        _atomic_write(_STATE_PATH, state)
        logger.info("llm: provider '%s' re-habilitado manualmente", provider)


# ── Cache SQLite ─────────────────────────────────────────────────────────────


def _ttl_seconds(purpose: str) -> float:
    table = {
        "sentiment": LLM.cache_ttl_sentiment_h,
        "event_score": LLM.cache_ttl_event_score_h,
        "assess_position": LLM.cache_ttl_assess_position_h,
        "narrative": LLM.cache_ttl_narrative_h,
        "wall_street": LLM.cache_ttl_wall_street_h,
        "risk_debate": LLM.cache_ttl_risk_debate_h,
    }
    return table.get(purpose, 1.0) * 3600.0


def cache_key(prompt: str, model: str, max_tokens: int, extra: str = "") -> str:
    payload = f"{model}|{max_tokens}|{extra}|{prompt}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@contextmanager
def _cache_conn():
    con = sqlite3.connect(str(_CACHE_DB_PATH), timeout=10.0)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=10000")
        yield con
        con.commit()
    finally:
        con.close()


_cache_initialized = False


def _ensure_cache_init() -> None:
    global _cache_initialized
    if _cache_initialized:
        return
    _CACHE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _cache_conn() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_cache (
                key         TEXT PRIMARY KEY,
                purpose     TEXT NOT NULL,
                provider    TEXT NOT NULL,
                model       TEXT NOT NULL,
                response    TEXT NOT NULL,
                tokens_in   INTEGER,
                tokens_out  INTEGER,
                created_at  REAL NOT NULL
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_cache_purpose ON llm_cache(purpose)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_cache_created ON llm_cache(created_at)")
    _cache_initialized = True


def cache_get(key: str, purpose: str) -> tuple[str, str, str] | None:
    """Devuelve (response, provider, model) si hay cache hit fresco."""
    _ensure_cache_init()
    cutoff = time.time() - _ttl_seconds(purpose)
    try:
        with _cache_conn() as con:
            row = con.execute(
                "SELECT response, provider, model FROM llm_cache WHERE key = ? AND created_at >= ?",
                (key, cutoff),
            ).fetchone()
            if row:
                return row[0], row[1], row[2]
    except sqlite3.Error as e:
        logger.warning("llm cache: error en get (%s)", e)
    return None


def cache_put(
    key: str,
    *,
    purpose: str,
    provider: str,
    model: str,
    response: str,
    tokens_in: int,
    tokens_out: int,
) -> None:
    _ensure_cache_init()
    try:
        with _cache_conn() as con:
            con.execute(
                "INSERT OR REPLACE INTO llm_cache "
                "(key, purpose, provider, model, response, tokens_in, tokens_out, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (key, purpose, provider, model, response, tokens_in, tokens_out, time.time()),
            )
    except sqlite3.Error as e:
        logger.warning("llm cache: error en put (%s)", e)


def cache_stats() -> dict:
    _ensure_cache_init()
    try:
        with _cache_conn() as con:
            total = con.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0]
            by_purpose = dict(con.execute("SELECT purpose, COUNT(*) FROM llm_cache GROUP BY purpose").fetchall())
            by_provider = dict(con.execute("SELECT provider, COUNT(*) FROM llm_cache GROUP BY provider").fetchall())
            return {"total_entries": total, "by_purpose": by_purpose, "by_provider": by_provider}
    except sqlite3.Error:
        return {"total_entries": 0, "by_purpose": {}, "by_provider": {}}


# ── Rate limiter local (token bucket por provider, in-process) ───────────────


_rate_lock = threading.Lock()
_rate_history: dict[str, deque[float]] = defaultdict(deque)


def _rate_limit_for(provider: str) -> int:
    return {
        "anthropic": LLM.rate_limit_anthropic_per_min,
        "groq": LLM.rate_limit_groq_per_min,
        "gemini": LLM.rate_limit_gemini_per_min,
        "deepseek": LLM.rate_limit_deepseek_per_min,
        "openrouter": LLM.rate_limit_openrouter_per_min,
    }.get(provider, 60)


def rate_acquire(provider: str) -> bool:
    """True si consume 1 slot del bucket de 60s. False si está saturado."""
    now = time.time()
    cutoff = now - 60.0
    limit = _rate_limit_for(provider)
    with _rate_lock:
        q = _rate_history[provider]
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


def rate_remaining(provider: str) -> int:
    now = time.time()
    cutoff = now - 60.0
    limit = _rate_limit_for(provider)
    with _rate_lock:
        recent = sum(1 for t in _rate_history[provider] if t >= cutoff)
        return max(0, limit - recent)


def rate_reset() -> None:
    """Reset usado por tests."""
    with _rate_lock:
        _rate_history.clear()


# ── Provider implementations (todos como funciones — un solo archivo) ────────
#
# Cada provider:
#   1. Verifica is_available (flag, env var, auto-disable).
#   2. Resuelve el modelo según el alias ("fast" | "deep" | "reasoning" | "default").
#   3. Invoca el SDK del provider.
#   4. Atrapa 4xx → desactiva 24h y levanta ProviderDisabled (sin retry).
#   5. Otros errores → levanta ProviderError (el gateway reintenta).
#   6. Devuelve ProviderResult con tokens, latencia y costo estimado.


# Lazy clients para no inicializar SDKs si no se usan.
_clients: dict[str, Any] = {}


def _disable_on_4xx(provider: str, status: int, exc: Exception) -> ProviderDisabled:
    reason = f"HTTP {status}: {exc}"
    disable_provider(provider, LLM.disable_provider_on_4xx_hours, reason)
    return ProviderDisabled(reason)


# Anthropic ───────────────────────────────────────────────────────────────────

def _anthropic_available() -> bool:
    if not LLM.enable_anthropic:
        return False
    if not os.getenv("ANTHROPIC_API_KEY"):
        return False
    disabled, _ = is_provider_disabled("anthropic")
    if disabled:
        return False
    return not is_budget_exhausted("anthropic")


def _anthropic_client():
    c = _clients.get("anthropic")
    if c is not None:
        return c
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ProviderDisabled("ANTHROPIC_API_KEY no presente")
    try:
        import anthropic  # type: ignore
    except ImportError as e:
        raise ProviderDisabled(f"anthropic SDK no instalado: {e}") from e
    c = anthropic.Anthropic(api_key=api_key)
    _clients["anthropic"] = c
    return c


def _call_anthropic(prompt: str, max_tokens: int, alias: str) -> ProviderResult:
    if not _anthropic_available():
        raise ProviderDisabled("anthropic no disponible")
    if alias == "deep":
        if not LLM.enable_sonnet:
            raise ProviderDisabled("sonnet flag OFF — enable_sonnet=True para activar")
        model = LLM.anthropic_deep_model
        rate = LLM.cost_per_mtok_anthropic_sonnet
    else:
        model = LLM.anthropic_fast_model
        rate = LLM.cost_per_mtok_anthropic_haiku
    client = _anthropic_client()
    start = time.perf_counter()
    try:
        msg = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        status = _extract_status_code(e)
        if status in (400, 401, 403):
            raise _disable_on_4xx("anthropic", status, e) from e
        raise ProviderError(f"anthropic call failed: {e}") from e
    latency_ms = int((time.perf_counter() - start) * 1000)
    text = msg.content[0].text if msg.content else ""
    usage = getattr(msg, "usage", None)
    tokens_in = getattr(usage, "input_tokens", 0) or 0
    tokens_out = getattr(usage, "output_tokens", 0) or 0
    cost = (tokens_in + tokens_out) / 1_000_000 * rate
    return ProviderResult(text, "anthropic", model, tokens_in, tokens_out, round(cost, 6), latency_ms)


# Groq ────────────────────────────────────────────────────────────────────────

def _groq_available() -> bool:
    if not os.getenv("GROQ_API_KEY"):
        return False
    disabled, _ = is_provider_disabled("groq")
    return not disabled


def _groq_client():
    c = _clients.get("groq")
    if c is not None:
        return c
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise ProviderDisabled("GROQ_API_KEY no presente")
    try:
        from groq import Groq  # type: ignore
    except ImportError as e:
        raise ProviderDisabled(f"groq SDK no instalado: {e}") from e
    c = Groq(api_key=api_key)
    _clients["groq"] = c
    return c


def _call_groq(prompt: str, max_tokens: int, alias: str) -> ProviderResult:
    if not _groq_available():
        raise ProviderDisabled("groq no disponible")
    model = LLM.groq_reasoning_model if alias in ("reasoning", "deep") else LLM.groq_fast_model
    client = _groq_client()
    start = time.perf_counter()
    try:
        resp = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        status = _extract_status_code(e)
        if status in (400, 401, 403):
            raise _disable_on_4xx("groq", status, e) from e
        raise ProviderError(f"groq call failed: {e}") from e
    latency_ms = int((time.perf_counter() - start) * 1000)
    text = resp.choices[0].message.content if resp.choices else ""
    usage = getattr(resp, "usage", None)
    tokens_in = getattr(usage, "prompt_tokens", 0) or 0
    tokens_out = getattr(usage, "completion_tokens", 0) or 0
    cost = (tokens_in + tokens_out) / 1_000_000 * LLM.cost_per_mtok_groq
    return ProviderResult(text or "", "groq", model, tokens_in, tokens_out, round(cost, 6), latency_ms)


# Gemini ──────────────────────────────────────────────────────────────────────

def _gemini_available() -> bool:
    if not os.getenv("GOOGLE_API_KEY"):
        return False
    disabled, _ = is_provider_disabled("gemini")
    return not disabled


def _gemini_client():
    c = _clients.get("gemini")
    if c is not None:
        return c
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise ProviderDisabled("GOOGLE_API_KEY no presente")
    try:
        from google import genai  # type: ignore
    except ImportError as e:
        raise ProviderDisabled(f"google-genai SDK no instalado: {e}") from e
    c = genai.Client(api_key=api_key)
    _clients["gemini"] = c
    return c


def _call_gemini(prompt: str, max_tokens: int, alias: str) -> ProviderResult:
    if not _gemini_available():
        raise ProviderDisabled("gemini no disponible")
    model = LLM.gemini_model
    client = _gemini_client()
    start = time.perf_counter()
    try:
        resp = client.models.generate_content(
            model=model,
            contents=prompt,
            config={"max_output_tokens": max_tokens},
        )
    except Exception as e:
        status = _extract_status_code(e)
        if status in (400, 401, 403):
            raise _disable_on_4xx("gemini", status, e) from e
        raise ProviderError(f"gemini call failed: {e}") from e
    latency_ms = int((time.perf_counter() - start) * 1000)
    text = getattr(resp, "text", "") or ""
    usage = getattr(resp, "usage_metadata", None)
    tokens_in = getattr(usage, "prompt_token_count", 0) or 0
    tokens_out = getattr(usage, "candidates_token_count", 0) or 0
    cost = (tokens_in + tokens_out) / 1_000_000 * LLM.cost_per_mtok_gemini
    return ProviderResult(text, "gemini", model, tokens_in, tokens_out, round(cost, 6), latency_ms)


# DeepSeek y OpenRouter — OpenAI-compatible ──────────────────────────────────

def _openai_compat_client(provider: str, env_var: str, base_url: str):
    c = _clients.get(provider)
    if c is not None:
        return c
    api_key = os.getenv(env_var)
    if not api_key:
        raise ProviderDisabled(f"{env_var} no presente")
    try:
        from openai import OpenAI  # type: ignore
    except ImportError as e:
        raise ProviderDisabled(f"openai SDK no instalado: {e}") from e
    c = OpenAI(api_key=api_key, base_url=base_url)
    _clients[provider] = c
    return c


def _openai_compat_call(
    provider: str,
    env_var: str,
    base_url: str,
    cost_per_mtok: float,
    model: str,
    prompt: str,
    max_tokens: int,
) -> ProviderResult:
    if not os.getenv(env_var):
        raise ProviderDisabled(f"{env_var} no presente")
    disabled, _ = is_provider_disabled(provider)
    if disabled:
        raise ProviderDisabled(f"{provider} auto-deshabilitado")
    client = _openai_compat_client(provider, env_var, base_url)
    start = time.perf_counter()
    try:
        resp = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        status = _extract_status_code(e)
        if status in (400, 401, 403):
            raise _disable_on_4xx(provider, status, e) from e
        raise ProviderError(f"{provider} call failed: {e}") from e
    latency_ms = int((time.perf_counter() - start) * 1000)
    text = resp.choices[0].message.content if resp.choices else ""
    usage = getattr(resp, "usage", None)
    tokens_in = getattr(usage, "prompt_tokens", 0) or 0
    tokens_out = getattr(usage, "completion_tokens", 0) or 0
    cost = (tokens_in + tokens_out) / 1_000_000 * cost_per_mtok
    return ProviderResult(text or "", provider, model, tokens_in, tokens_out, round(cost, 6), latency_ms)


def _call_deepseek(prompt: str, max_tokens: int, alias: str) -> ProviderResult:
    model = LLM.deepseek_reasoning_model if alias in ("reasoning", "deep") else LLM.deepseek_chat_model
    return _openai_compat_call(
        "deepseek", "DEEPSEEK_API_KEY", "https://api.deepseek.com/v1",
        LLM.cost_per_mtok_deepseek, model, prompt, max_tokens,
    )


def _call_openrouter(prompt: str, max_tokens: int, alias: str) -> ProviderResult:
    model = LLM.openrouter_reasoning_model if alias in ("reasoning", "deep") else LLM.openrouter_fast_model
    return _openai_compat_call(
        "openrouter", "OPENROUTER_API_KEY", "https://openrouter.ai/api/v1",
        LLM.cost_per_mtok_openrouter, model, prompt, max_tokens,
    )


# Registry y resolver de modelos ──────────────────────────────────────────────

_PROVIDERS = {
    "anthropic": _call_anthropic,
    "groq": _call_groq,
    "gemini": _call_gemini,
    "deepseek": _call_deepseek,
    "openrouter": _call_openrouter,
}

_AVAILABILITY = {
    "anthropic": _anthropic_available,
    "groq": _groq_available,
    "gemini": _gemini_available,
    "deepseek": lambda: bool(os.getenv("DEEPSEEK_API_KEY")) and not is_provider_disabled("deepseek")[0],
    "openrouter": lambda: bool(os.getenv("OPENROUTER_API_KEY")) and not is_provider_disabled("openrouter")[0],
}


def _resolve_model_name(provider: str, alias: str) -> str:
    """Nombre exacto del modelo — usado en el cache key."""
    table = {
        ("anthropic", "fast"): LLM.anthropic_fast_model,
        ("anthropic", "deep"): LLM.anthropic_deep_model,
        ("groq", "fast"): LLM.groq_fast_model,
        ("groq", "reasoning"): LLM.groq_reasoning_model,
        ("gemini", "default"): LLM.gemini_model,
        ("deepseek", "fast"): LLM.deepseek_chat_model,
        ("deepseek", "reasoning"): LLM.deepseek_reasoning_model,
        ("openrouter", "fast"): LLM.openrouter_fast_model,
        ("openrouter", "reasoning"): LLM.openrouter_reasoning_model,
    }
    return table.get((provider, alias), f"{provider}/{alias}")


# ── Gateway core ─────────────────────────────────────────────────────────────


def _try_provider(provider_id: str, alias: str, prompt: str, max_tokens: int) -> ProviderResult | None:
    """Intenta llamar al provider con backoff. None si hay que pasar al siguiente."""
    call_fn = _PROVIDERS.get(provider_id)
    if call_fn is None:
        return None
    avail_fn = _AVAILABILITY.get(provider_id)
    try:
        if avail_fn and not avail_fn():
            return None
    except Exception as e:
        logger.debug("provider %s availability check error: %s", provider_id, e)
        return None
    if not rate_acquire(provider_id):
        logger.info("rate_limit local saturado para %s — siguiente provider", provider_id)
        return None

    last_error: Exception | None = None
    for attempt in range(LLM.retry_max_attempts + 1):
        try:
            return call_fn(prompt, max_tokens, alias)
        except ProviderDisabled as e:
            logger.info("provider %s disabled: %s", provider_id, e)
            return None
        except ProviderError as e:
            last_error = e
            if attempt < LLM.retry_max_attempts:
                wait = LLM.retry_backoff_seconds[min(attempt, len(LLM.retry_backoff_seconds) - 1)]
                logger.info("provider %s falló intento %d (%s) — backoff %ss", provider_id, attempt + 1, e, wait)
                time.sleep(wait)
                continue
            logger.warning("provider %s agotó retries: %s", provider_id, e)
            return None
    if last_error:
        logger.warning("provider %s falló definitivo: %s", provider_id, last_error)
    return None


def call_llm(
    prompt: str,
    *,
    purpose: Purpose,
    max_tokens: int = 200,
    cache_key_extra: str = "",
) -> str | None:
    """Llamada LLM unificada con cache, fallback, budget y rate limit.

    Args:
        prompt: texto del prompt.
        purpose: categoría — define cascada de providers, TTL del cache y budget.
        max_tokens: límite duro de tokens de salida.
        cache_key_extra: texto extra que entra al SHA del cache key (típicamente
            el ticker o un identificador del contexto para discriminar respuestas
            similares).

    Returns:
        El texto de la respuesta, o None si todos los providers fallaron y no
        hay cache. El caller debe tener un fallback determinista (heurística,
        keywords, default) cuando reciba None.
    """
    cascade = LLM_CASCADE_BY_PURPOSE.get(purpose, [])
    if not cascade:
        logger.warning("call_llm: purpose '%s' sin cascada definida", purpose)
        return None

    # 1. Cache lookup — probamos cada (provider, alias) por si alguna corrida
    # anterior cacheó la respuesta.
    for provider_id, alias in cascade:
        model = _resolve_model_name(provider_id, alias)
        key = cache_key(prompt, model, max_tokens, cache_key_extra)
        hit = cache_get(key, purpose)
        if hit:
            text, cached_provider, cached_model = hit
            record_call(cached_provider, tokens_in=0, tokens_out=0, cost_usd=0.0, cache_hit=True)
            logger.debug(
                "llm cache hit: %s/%s purpose=%s key=%s",
                cached_provider, cached_model, purpose, key[:8],
            )
            return text

    # 2. Cascada de providers.
    fallback_from: str | None = None
    for provider_id, alias in cascade:
        if is_budget_exhausted(provider_id):
            logger.info("budget exhausted para %s — siguiente provider", provider_id)
            fallback_from = provider_id
            continue
        result = _try_provider(provider_id, alias, prompt, max_tokens)
        if result is None:
            fallback_from = provider_id
            continue
        record_call(
            result.provider,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            cost_usd=result.cost_usd,
            cache_hit=False,
            fallback_from=fallback_from if fallback_from and fallback_from != provider_id else None,
        )
        model = _resolve_model_name(provider_id, alias)
        key = cache_key(prompt, model, max_tokens, cache_key_extra)
        cache_put(
            key,
            purpose=purpose, provider=result.provider, model=result.model, response=result.text,
            tokens_in=result.tokens_in, tokens_out=result.tokens_out,
        )
        logger.info(
            "llm: %s/%s purpose=%s tok=%d→%d %dms cost=$%.5f",
            result.provider, result.model, purpose,
            result.tokens_in, result.tokens_out, result.latency_ms, result.cost_usd,
        )
        return result.text

    logger.warning("llm: todos los providers fallaron para purpose=%s", purpose)
    return None


def get_gateway_status() -> dict[str, Any]:
    """Snapshot del estado del gateway para dashboard y bot Telegram."""
    budget = _load_budget()
    state = _load_state()
    providers_status = {}
    for pid in _PROVIDERS.keys():
        try:
            available = _AVAILABILITY[pid]()
        except Exception:
            available = False
        disabled, reason = is_provider_disabled(pid)
        providers_status[pid] = {
            "available": available,
            "auto_disabled": disabled,
            "disabled_reason": reason,
            "rate_remaining_per_min": rate_remaining(pid),
        }
    return {
        "date": budget["date"],
        "providers": budget["providers"],
        "total_cost_usd": round(sum(p.get("cost_usd", 0.0) for p in budget["providers"].values()), 4),
        "total_calls": sum(p.get("calls", 0) for p in budget["providers"].values()),
        "anthropic_budget_used_pct": round(
            100.0 * get_today_cost("anthropic") / max(LLM.daily_anthropic_budget_usd, 1e-9), 1
        ),
        "total_budget_used_pct": round(
            100.0 * get_today_cost(None) / max(LLM.daily_total_budget_usd, 1e-9), 1
        ),
        "disabled_providers": state.get("disabled", {}),
        "providers_status": providers_status,
        "cache_stats": cache_stats(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Funciones públicas legacy — el resto del sistema las sigue llamando igual.
# Internamente ahora usan call_llm() con el purpose correspondiente.
# ─────────────────────────────────────────────────────────────────────────────


PositionAction = Literal["CLOSE", "HOLD", "REDUCE"]


def assess_position(
    ticker: str,
    current_price: float,
    entry_price: float,
    pnl_pct: float,
    stop_loss: float | None,
    news_headlines: list[str],
    macro_regime: str,
) -> dict | None:
    """¿CLOSE, HOLD, o REDUCE? Devuelve dict con action/confidence/reason o None."""
    news_block = (
        "\n".join(f"- {h}" for h in news_headlines[:6])
        if news_headlines
        else "No recent news available."
    )
    sl_text = f"${stop_loss:.2f}" if stop_loss else "N/A (dynamic trailing)"
    prompt = f"""You are a systematic risk manager at a quant fund. Evaluate this open equity position.

POSITION
  Ticker: {ticker}
  Entry: ${entry_price:.2f}  |  Current: ${current_price:.2f}  |  P&L: {pnl_pct:+.1f}%
  Hard stop: {sl_text}
  Market regime: {macro_regime.upper()}

RECENT NEWS FOR {ticker}
{news_block}

Decide: should we CLOSE, HOLD, or REDUCE (sell half)?

Rules:
- CLOSE: news shows fundamental deterioration, fraud, sector systemic shock, or geopolitical event directly harming this name.
- REDUCE: mixed signals — some negative news but position thesis still partly valid.
- HOLD: normal volatility, no new negative catalysts, thesis intact.
- If P&L > -1% and no bad news → always HOLD.
- Ignore noise; focus on material events that change the 3-6 month outlook.

Respond with JSON only, no prose:
{{"action": "CLOSE"|"HOLD"|"REDUCE", "confidence": 0.0-1.0, "reason": "≤15 words"}}"""

    text = call_llm(prompt, purpose="assess_position", max_tokens=80, cache_key_extra=ticker)
    if not text:
        return None
    match = re.search(r"\{.*?\}", text, re.DOTALL)
    if not match:
        return None
    try:
        result = json.loads(match.group())
    except json.JSONDecodeError:
        return None
    action = result.get("action", "HOLD")
    if action not in ("CLOSE", "HOLD", "REDUCE"):
        action = "HOLD"
    return {
        "action": action,
        "confidence": float(result.get("confidence", 0.5)),
        "reason": str(result.get("reason", "")),
    }


def build_macro_narrative(
    macro: dict,
    top_signals: list[dict],
    radar_entries: list[dict],
) -> str | None:
    """2 frases sobre el régimen para el WhatsApp brief. None si LLM falla."""
    regime = macro.get("regime", "unknown").upper()
    vix = macro.get("prices", {}).get("vix", "?")
    wti = macro.get("prices", {}).get("oil_wti", "?")
    gold = macro.get("prices", {}).get("gold", "?")
    tickers_picked = [s.get("ticker") for s in top_signals[:4]]
    top_movers = [
        f"{e.get('ticker')} {e.get('move_pct', 0):+.1f}%"
        for e in radar_entries[:5]
        if e.get("move_pct") is not None
    ]
    prompt = f"""You are a concise financial analyst. Write EXACTLY 2 sentences summarizing today's market for a Latin American investor.

Data:
- Regime: {regime}
- VIX: {vix} | WTI: ${wti} | Gold: ${gold}
- Portfolio picks today: {', '.join(tickers_picked) if tickers_picked else 'none'}
- Top movers in universe: {', '.join(top_movers) if top_movers else 'none'}

Tone: direct, no fluff. Mention the most important risk or opportunity.
Write in Spanish. MAX 40 words total. No emojis."""

    cache_extra = f"{regime}|{vix}|{','.join(tickers_picked)}"
    return call_llm(prompt, purpose="narrative", max_tokens=80, cache_key_extra=cache_extra)


def wall_street_analysis(
    ticker: str,
    fundamentals: dict,
    quant: dict,
    news_headlines: list[str],
    macro_regime: str,
    *,
    sector: str = "Other",
) -> dict | None:
    """Tesis completa de inversión tipo analista senior. None si LLM falla."""
    from alpha_agent.analytics.fundamental import format_for_claude
    fundamental_block = format_for_claude(ticker, fundamentals, quant=quant)
    news_block = (
        "\n".join(f"- {h}" for h in news_headlines[:5])
        if news_headlines
        else "No hay noticias recientes disponibles."
    )
    prompt = f"""Sos un analista senior de renta variable en una gestora de primer nivel de Wall Street.
Analizá {ticker} con la siguiente información y generá una tesis de inversión completa.

{fundamental_block}

NOTICIAS RECIENTES:
{news_block}

CONTEXTO MACRO: Régimen {macro_regime.upper()}

Generá un análisis estructurado. Respondé con JSON válido únicamente:
{{
  "thesis": "2-3 líneas: por qué comprar/vender/mantener ahora",
  "catalysts": "próximos 1-3 catalizadores concretos que podrían mover el precio",
  "risks": "1-2 riesgos específicos y cuantificados si es posible",
  "valuation": "CARO|JUSTO|BARATO — 1 línea de justificación vs sector/histórico",
  "recommendation": "BUY|HOLD|SELL",
  "price_target_pct": 12.5
}}

price_target_pct es el upside/downside esperado en % a 12 meses (puede ser negativo).
Sé concreto y directo. No uses frases vagas."""

    text = call_llm(prompt, purpose="wall_street", max_tokens=500, cache_key_extra=ticker)
    if not text:
        return None
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        result = json.loads(match.group())
    except json.JSONDecodeError:
        return None
    if result.get("recommendation") not in ("BUY", "HOLD", "SELL"):
        result["recommendation"] = "HOLD"
    return result


def risk_debate(
    ticker: str,
    signal: dict,
    portfolio_context: dict,
) -> dict:
    """Debate bull/bear pre-ejecución. Devuelve siempre un dict (default PROCEED si LLM falla)."""
    default = {
        "bull_case": "AI no disponible — usando señal original",
        "bear_case": "Sin análisis",
        "verdict": "PROCEED",
        "confidence": 0.5,
        "size_adjustment": 1.0,
    }
    regime = portfolio_context.get("regime", "unknown")
    vix = portfolio_context.get("vix", 20)
    positions = portfolio_context.get("current_positions", [])
    capital = portfolio_context.get("capital_usd", 1600)
    conv = signal.get("thesis", {}).get("conviction", "MEDIA")
    sharpe = signal.get("thesis", {}).get("quant", {}).get("sharpe", 0) or 0
    alpha = (signal.get("thesis", {}).get("quant", {}).get("alpha_jensen", 0) or 0) * 100
    stop = signal.get("stop_loss")
    tp = signal.get("take_profit")
    price = signal.get("price", 0)
    r_r = ((tp - price) / (price - stop)) if (tp and stop and price and price > stop) else None
    prompt = (
        f"Sos un risk arbitrage committee evaluando si ejecutar esta señal.\n\n"
        f"SEÑAL: {ticker}\n"
        f"- Conviction: {conv} | Sharpe: {sharpe:.2f} | Alpha Jensen: {alpha:+.1f}%\n"
        f"- Precio: ${price:.2f} | Stop: ${f'{stop:.2f}' if stop else 'N/D'} | TP: ${f'{tp:.2f}' if tp else 'N/D'}\n"
        f"- R/R implícito: {f'{r_r:.1f}x' if r_r else 'N/D'}\n\n"
        f"CONTEXTO: Régimen {regime.upper()} | VIX {vix:.1f} | Capital ${capital:.0f}\n"
        f"Posiciones actuales: {', '.join(positions) if positions else 'ninguna'}\n\n"
        'Respondé JSON únicamente: {"bull_case":"...","bear_case":"...","verdict":"PROCEED|REDUCE_SIZE|SKIP","confidence":0.0-1.0,"size_adjustment":1.0}'
    )
    text = call_llm(prompt, purpose="risk_debate", max_tokens=200, cache_key_extra=ticker)
    if not text:
        return default
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return default
    try:
        result = json.loads(match.group())
    except json.JSONDecodeError:
        return default
    if result.get("verdict") not in ("PROCEED", "REDUCE_SIZE", "SKIP"):
        result["verdict"] = "PROCEED"
    result["size_adjustment"] = max(0.0, min(1.0, float(result.get("size_adjustment", 1.0))))
    return result


def score_event_impact(ticker: str, headline: str, sector: str) -> int:
    """Clasificación de un headline: 1 (bullish) | 0 (neutral) | -1 (bearish)."""
    prompt = (
        f'Financial news: "{headline}"\n'
        f"Stock: {ticker} (sector: {sector})\n"
        "Impact on stock price next week? Reply ONLY with: 1, 0, or -1"
    )
    text = call_llm(prompt, purpose="event_score", max_tokens=5, cache_key_extra=f"{ticker}|{headline}")
    if not text:
        return 0
    match = re.search(r"-?1|0", text.strip())
    if not match:
        return 0
    try:
        return max(-1, min(1, int(match.group())))
    except ValueError:
        return 0
