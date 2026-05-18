"""Gateway LLM — punto de entrada único para todas las llamadas a modelos.

Orquesta:
1. **Cache** (`alpha_agent.llm.cache`): si la respuesta a este prompt+modelo
   ya existe y no está expirada por el TTL del `purpose`, la devuelve sin
   tocar la red.
2. **Cascada de providers**: definida en `config.LLM_CASCADE_BY_PURPOSE`.
   Para cada (provider, alias) en orden, verifica disponibilidad, rate limit
   local y budget; si todo OK, invoca. Si falla con error transitorio (5xx,
   timeout, rate limit del servidor), reintenta con backoff. Si falla con
   4xx (auth/billing/abuse), el provider se auto-deshabilita 24h sin retry
   y se cae al siguiente.
3. **Budget tracking** (`alpha_agent.llm.budget`): cada llamada exitosa o
   cache hit se registra. Si Anthropic supera `daily_anthropic_budget_usd`
   o el total supera `daily_total_budget_usd`, el kill switch activa.
4. **Telemetría**: cada llamada loguea provider, modelo, tokens, latencia,
   cache hit, fallback. Snapshot diario archivado en `logs/llm_usage_*.json`
   por el budget tracker.

API mínima — el resto del código sólo debería llamar a `call_llm()`:

    from alpha_agent.llm import call_llm

    text = call_llm(
        prompt="Clasificá: \"NVDA beats earnings...\"",
        purpose="event_score",
        max_tokens=10,
        cache_key_extra="NVDA",
    )
"""

from __future__ import annotations

import logging
import time
from typing import Literal

from alpha_agent.config import LLM, LLM_CASCADE_BY_PURPOSE
from alpha_agent.llm import budget, cache, rate_limit
from alpha_agent.llm.providers import ProviderDisabled, ProviderError, ProviderResult
from alpha_agent.llm.providers.anthropic_provider import AnthropicProvider
from alpha_agent.llm.providers.deepseek_provider import DeepSeekProvider
from alpha_agent.llm.providers.gemini_provider import GeminiProvider
from alpha_agent.llm.providers.groq_provider import GroqProvider
from alpha_agent.llm.providers.openrouter_provider import OpenRouterProvider

logger = logging.getLogger(__name__)

Purpose = Literal[
    "sentiment",
    "event_score",
    "assess_position",
    "narrative",
    "wall_street",
    "risk_debate",
]


# ── Registro singleton de providers ──────────────────────────────────────────


_PROVIDERS = {
    "anthropic": AnthropicProvider(),
    "groq": GroqProvider(),
    "gemini": GeminiProvider(),
    "deepseek": DeepSeekProvider(),
    "openrouter": OpenRouterProvider(),
}


def _resolve_model_name(provider: str, alias: str) -> str:
    """Devuelve el nombre exacto del modelo para el cache key."""
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


def _try_provider(
    provider_id: str,
    alias: str,
    prompt: str,
    max_tokens: int,
) -> ProviderResult | None:
    """Intenta llamar al provider con backoff. Devuelve None si hay que pasar al siguiente."""
    provider = _PROVIDERS.get(provider_id)
    if provider is None:
        return None

    # Pre-flight: disponibilidad estructural.
    try:
        if not provider.is_available():
            return None
    except Exception as e:
        logger.debug("provider %s is_available() error: %s", provider_id, e)
        return None

    # Rate limit local.
    if not rate_limit.try_acquire(provider_id):
        logger.info("rate_limit local saturado para %s — siguiente provider", provider_id)
        return None

    last_error: Exception | None = None
    for attempt in range(LLM.retry_max_attempts + 1):
        try:
            return provider.call(prompt, max_tokens=max_tokens, model_alias=alias)
        except ProviderDisabled as e:
            # Provider apagado (flag, key, auto-disable). NO retry — siguiente.
            logger.info("provider %s disabled: %s", provider_id, e)
            return None
        except ProviderError as e:
            last_error = e
            if attempt < LLM.retry_max_attempts:
                wait = LLM.retry_backoff_seconds[min(attempt, len(LLM.retry_backoff_seconds) - 1)]
                logger.info(
                    "provider %s falló intento %d (%s) — backoff %ss",
                    provider_id, attempt + 1, e, wait,
                )
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

    # 1. Cache lookup — probamos todos los providers de la cascada por si alguna
    # corrida anterior cacheó la respuesta con otro provider.
    for provider_id, alias in cascade:
        model = _resolve_model_name(provider_id, alias)
        key = cache.cache_key(prompt, model, max_tokens, cache_key_extra)
        hit = cache.get(key, purpose)
        if hit:
            text, cached_provider, cached_model = hit
            budget.record_call(
                cached_provider,
                tokens_in=0,
                tokens_out=0,
                cost_usd=0.0,
                cache_hit=True,
            )
            logger.debug(
                "llm cache hit: %s/%s purpose=%s key=%s",
                cached_provider, cached_model, purpose, key[:8],
            )
            return text

    # 2. Cascada de providers.
    fallback_from: str | None = None
    for provider_id, alias in cascade:
        # Budget global kill switch.
        if budget.is_budget_exhausted(provider_id):
            logger.info("budget exhausted para %s — siguiente provider", provider_id)
            fallback_from = provider_id
            continue

        result = _try_provider(provider_id, alias, prompt, max_tokens)
        if result is None:
            fallback_from = provider_id
            continue

        # Hit — registrar en budget y cache.
        budget.record_call(
            result.provider,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            cost_usd=result.cost_usd,
            cache_hit=False,
            fallback_from=fallback_from if fallback_from and fallback_from != provider_id else None,
        )
        model = _resolve_model_name(provider_id, alias)
        key = cache.cache_key(prompt, model, max_tokens, cache_key_extra)
        cache.put(
            key,
            purpose=purpose,
            provider=result.provider,
            model=result.model,
            response=result.text,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
        )
        logger.info(
            "llm: %s/%s purpose=%s tok=%d→%d %dms cost=$%.5f",
            result.provider, result.model, purpose,
            result.tokens_in, result.tokens_out, result.latency_ms, result.cost_usd,
        )
        return result.text

    logger.warning("llm: todos los providers fallaron para purpose=%s", purpose)
    return None


def get_gateway_status() -> dict:
    """Snapshot del estado para el dashboard y comandos Telegram."""
    snap = budget.get_snapshot()
    providers_status = {}
    for pid, prov in _PROVIDERS.items():
        try:
            available = prov.is_available()
        except Exception:
            available = False
        disabled, reason = budget.is_provider_disabled(pid)
        providers_status[pid] = {
            "available": available,
            "auto_disabled": disabled,
            "disabled_reason": reason,
            "rate_remaining_per_min": rate_limit.get_remaining(pid),
        }
    snap["providers_status"] = providers_status
    snap["cache_stats"] = cache.get_stats()
    return snap
