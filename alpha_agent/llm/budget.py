"""Budget tracking diario + estado de auto-disable por provider.

Persiste en dos archivos:

- `signals/llm_budget.json`:  calls, tokens y costo USD por provider del día
  actual (UTC). Se resetea automáticamente al cambiar de fecha. El gateway lo
  consulta antes de cada llamada para decidir si activar el kill switch
  (`daily_anthropic_budget_usd`, `daily_total_budget_usd`).

- `signals/llm_provider_state.json`: timestamp hasta cuándo cada provider
  está auto-deshabilitado tras un 400/401/403. Cloud Run Jobs son efímeros
  pero el JSON se commitea al repo, así que el estado persiste entre
  corridas.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from alpha_agent.config import LLM, PATHS

logger = logging.getLogger(__name__)

_BUDGET_PATH = PATHS.signals_dir / "llm_budget.json"
_STATE_PATH = PATHS.signals_dir / "llm_provider_state.json"


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return dict(default)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("llm_budget: archivo corrupto %s (%s) — reset.", path.name, e)
        return dict(default)


def _atomic_write(path: Path, data: dict) -> None:
    """Escribe via tempfile + replace para evitar corrupción si el proceso muere."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


# ── Budget diario ─────────────────────────────────────────────────────────────


def _load_budget() -> dict:
    data = _load_json(_BUDGET_PATH, {"date": _today_utc(), "providers": {}})
    if data.get("date") != _today_utc():
        # Día nuevo — archivar el anterior en logs y resetear.
        logger.info("llm_budget: reset diario (era %s, ahora %s)", data.get("date"), _today_utc())
        archive = PATHS.logs_dir / f"llm_usage_{data.get('date', 'unknown')}.json"
        try:
            _atomic_write(archive, data)
        except OSError as e:
            logger.warning("llm_budget: no se pudo archivar %s (%s)", archive.name, e)
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
        {
            "calls": 0,
            "cache_hits": 0,
            "tokens_in": 0,
            "tokens_out": 0,
            "cost_usd": 0.0,
            "fallbacks_in": 0,
        },
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


def budget_remaining(provider: str | None = None) -> float:
    """USD restantes hoy. Negativo si ya excedió."""
    if provider == "anthropic":
        cap = LLM.daily_anthropic_budget_usd
    else:
        cap = LLM.daily_total_budget_usd
    return cap - get_today_cost(provider)


def is_budget_exhausted(provider: str) -> bool:
    """¿Se acabó el presupuesto para este provider (o el total)?"""
    if provider == "anthropic" and get_today_cost("anthropic") >= LLM.daily_anthropic_budget_usd:
        return True
    return get_today_cost(None) >= LLM.daily_total_budget_usd


# ── Auto-disable por errores 4xx ──────────────────────────────────────────────


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
    logger.warning(
        "llm_budget: provider '%s' deshabilitado hasta %s — %s",
        provider, until, reason,
    )


def is_provider_disabled(provider: str) -> tuple[bool, str | None]:
    """Devuelve (disabled, reason). reason es None si está activo."""
    state = _load_state()
    entry = state["disabled"].get(provider)
    if not entry:
        return False, None
    try:
        until = datetime.fromisoformat(entry["until"])
    except (ValueError, TypeError):
        return False, None
    if datetime.now(timezone.utc) >= until:
        # Expiró — limpiar.
        state["disabled"].pop(provider, None)
        _atomic_write(_STATE_PATH, state)
        return False, None
    return True, entry.get("reason")


def enable_provider(provider: str) -> None:
    """Re-habilita un provider manualmente (saca el auto-disable)."""
    state = _load_state()
    if state["disabled"].pop(provider, None) is not None:
        _atomic_write(_STATE_PATH, state)
        logger.info("llm_budget: provider '%s' re-habilitado manualmente", provider)


# ── Snapshot para dashboard/Telegram ──────────────────────────────────────────


def get_snapshot() -> dict[str, Any]:
    """Devuelve un dict navegable con el estado actual para el dashboard."""
    budget = _load_budget()
    state = _load_state()
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
    }
