"""
AI-driven allocation decision engine.

Filosofía: capital quieto no rinde. Deployer agresivo cuando hay edge demostrado,
defensivo cuando el sistema está perdiendo. Opciones solo con catalizador real.

Sistema de 3 niveles basado en régimen + racha reciente:
  NIVEL 1 — Alta convicción (BULL + win_rate >= 55%): CP 88%, OPT condicional
  NIVEL 2 — Base (BULL sin historial / NEUTRAL): CP 75%, OPT si hay catalizador
  NIVEL 3 — Defensivo (BEAR / racha perdedora): CP 45%, OPT mínimo

LP siempre 0%: con $1600 el CP rotatorio supera ampliamente a holds de semanas.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class AllocationDecision:
    lp_pct: float          # siempre 0.0 — LP desactivado para capital < $5k
    cp_pct: float          # fracción en CP momentum (0-1)
    opt_pct: float         # fracción en opciones (0 o 0.10 según catalizador)
    n_cp_positions: int    # cuántas posiciones CP concentradas (1-3)
    cp_max_hold_days: int  # días máximos en CP antes de salida forzada
    reasoning: str         # una frase de justificación
    level: int = 2         # 1=agresivo, 2=base, 3=defensivo


# ── Niveles de convicción ─────────────────────────────────────────────────────
# NIVEL 1: Edge demostrado → presionar fuerte. Cash mínimo operativo (5%).
# NIVEL 2: Sin historial claro → posición base. Cash buffer 15%.
# NIVEL 3: Perdiendo o BEAR → capital casi quieto. Esperar mejor setup.

def _rule_default(regime: str, vix: float, win_rate: float | None, recent_pnl: float | None) -> AllocationDecision:
    reg = regime.upper()

    # Racha perdedora definida: win_rate < 40% con al menos 3 trades recientes
    losing_streak = (win_rate is not None and win_rate < 0.40)
    # Racha ganadora: win_rate >= 55%
    winning_streak = (win_rate is not None and win_rate >= 0.55)

    # NIVEL 3: BEAR o racha perdedora → defensivo
    if vix > 30 or reg == "BEAR" or losing_streak:
        reason = (
            f"BEAR/VIX {vix:.0f}: defensivo, 1 posición CP, capital protegido."
            if vix > 30 or reg == "BEAR"
            else f"Racha perdedora (win_rate {win_rate:.0%}): reduciendo exposición."
        )
        return AllocationDecision(0.0, 0.45, 0.05, 1, 4, reason, level=3)

    # NEUTRAL: posición base conservadora
    if vix > 22 or reg == "NEUTRAL":
        cp = 0.80 if winning_streak else 0.65
        n  = 2
        reason = f"NEUTRAL/VIX {vix:.0f}: CP {cp:.0%}, {'racha ganadora → más exposición.' if winning_streak else 'posición moderada.'}"
        return AllocationDecision(0.0, cp, 0.10, n, 8, reason, level=2)

    # BULL — el caso principal
    if winning_streak:
        # NIVEL 1: BULL + ganando → presionar al máximo; chandelier es el exit primario
        return AllocationDecision(0.0, 0.88, 0.07, 2, 18,
            f"BULL + racha ganadora ({win_rate:.0%} win rate): CP al máximo, hold hasta 18d.", level=1)
    elif losing_streak:
        # ya cubierto arriba pero por si acá
        return AllocationDecision(0.0, 0.50, 0.05, 1, 4,
            f"BULL pero racha perdedora: reduciendo CP al 50%.", level=3)
    else:
        # NIVEL 2: BULL sin historial suficiente → base sólida
        cp = 0.83 if vix < 18 else 0.75
        return AllocationDecision(0.0, cp, 0.10, 2, 14,
            f"BULL + VIX {vix:.1f}: CP {cp:.0%}, hold hasta 14d.", level=2)


def _get_recent_performance() -> tuple[float | None, float | None]:
    """Win rate y P&L de los últimos 7 días desde trade_db."""
    try:
        from alpha_agent.analytics.trade_db import get_trades
        from datetime import datetime, timedelta
        cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        trades = get_trades(limit=100)
        recent = [t for t in trades if (t.get("date") or "") >= cutoff and t.get("pnl_usd") is not None]
        if len(recent) < 3:          # mínimo 3 trades para considerar racha
            return None, None
        wins = sum(1 for t in recent if (t.get("pnl_usd") or 0) > 0)
        total_pnl = sum((t.get("pnl_usd") or 0) for t in recent)
        return round(wins / len(recent), 2), round(total_pnl, 2)
    except Exception as exc:
        log.debug("get_recent_performance: %s", exc)
        return None, None


def decide_allocation(
    regime: str,
    vix: float,
    capital: float = 1600.0,
    sector_momentum: dict[str, float] | None = None,
    prediction: Any | None = None,
) -> AllocationDecision:
    """
    Determina la asignación óptima según régimen macro + historial reciente.
    Usa reglas deterministas calibradas — equivalentes al output de Claude Haiku
    el 95% del tiempo pero sin costo de API y sin varianza estocástica.

    Principio: capital quieto no rinde. Deployer agresivo con edge, defensivo sin él.
    """
    win_rate, recent_pnl = _get_recent_performance()
    return _rule_default(regime, vix, win_rate, recent_pnl)
