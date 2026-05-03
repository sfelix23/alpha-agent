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
        return AllocationDecision(0.0, 0.45, 0.05, 1, 2, reason, level=3)

    # NEUTRAL: posición base conservadora
    if vix > 22 or reg == "NEUTRAL":
        cp = 0.80 if winning_streak else 0.65
        n  = 2
        reason = f"NEUTRAL/VIX {vix:.0f}: CP {cp:.0%}, {'racha ganadora → más exposición.' if winning_streak else 'posición moderada.'}"
        return AllocationDecision(0.0, cp, 0.10, n, 3, reason, level=2)

    # BULL — el caso principal
    if winning_streak:
        # NIVEL 1: BULL + ganando → presionar al máximo
        return AllocationDecision(0.0, 0.88, 0.07, 2, 3,
            f"BULL + racha ganadora ({win_rate:.0%} win rate): CP al máximo, cash mínimo.", level=1)
    elif losing_streak:
        # ya cubierto arriba pero por si acá
        return AllocationDecision(0.0, 0.50, 0.05, 1, 2,
            f"BULL pero racha perdedora: reduciendo CP al 50%.", level=3)
    else:
        # NIVEL 2: BULL sin historial suficiente → base sólida
        cp = 0.83 if vix < 18 else 0.75
        return AllocationDecision(0.0, cp, 0.10, 2, 3,
            f"BULL + VIX {vix:.1f}: CP {cp:.0%}, 2 posiciones, opciones activas.", level=2)


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
    Claude Haiku decide la asignación óptima según contexto macro + historial.
    Fallback a reglas si la API no está disponible.

    Principio: capital quieto no rinde. Deployer agresivo con edge, defensivo sin él.
    """
    win_rate, recent_pnl = _get_recent_performance()

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        log.warning("ANTHROPIC_API_KEY not set — using rule-based allocation")
        return _rule_default(regime, vix, win_rate, recent_pnl)

    ctx: dict[str, Any] = {
        "regime": regime,
        "vix": round(vix, 1),
        "capital_usd": capital,
    }
    if win_rate is not None:
        ctx["recent_win_rate_7d"] = win_rate
        ctx["recent_pnl_7d_usd"] = recent_pnl
        ctx["streak"] = (
            "WINNING" if win_rate >= 0.55
            else "LOSING" if win_rate < 0.40
            else "NEUTRAL"
        )
    if sector_momentum:
        top = sorted(sector_momentum.items(), key=lambda x: x[1], reverse=True)[:3]
        ctx["top_sectors_momentum"] = {k: round(v, 3) for k, v in top}
    if prediction is not None:
        ctx["market_prediction"] = {
            "direction": prediction.direction,
            "conviction": round(prediction.conviction, 2),
            "score": prediction.score,
        }

    prompt = (
        "You are the allocation AI for a $1600 concentrated momentum trading account.\n"
        "Goal: maximize capital growth rate. Idle cash does NOT help — deploy aggressively when edge exists.\n\n"
        f"Market context:\n{json.dumps(ctx, indent=2)}\n\n"
        "RULES (strict):\n"
        "- lp_pct = 0.0 always (LP disabled — capital too small, CP momentum is superior)\n"
        "- opt_pct: 0.10 if market has catalyst/vol setup; 0.05 if no clear options edge\n"
        "- cp_pct + opt_pct <= 0.95 (keep min 5% cash for operational margin)\n"
        "- cp_pct range: 0.40 (defensive) to 0.88 (max aggression)\n\n"
        "CONVICTION LEVELS:\n"
        "- BULL + streak=WINNING: cp_pct=0.88, n_cp_positions=2, cp_max_hold_days=3 — press the edge\n"
        "- BULL + streak=NEUTRAL (no clear edge): cp_pct=0.78-0.83, 2 positions, 3 days\n"
        "- BULL + streak=LOSING: cp_pct=0.55, 1 position, 2 days — wait for better setup\n"
        "- NEUTRAL or VIX 22-30: cp_pct=0.65-0.75, 2 positions, 3 days\n"
        "- BEAR or VIX>30 or streak=LOSING: cp_pct=0.40-0.50, 1 position, 2 days\n\n"
        "Respond ONLY with JSON (no markdown):\n"
        '{"lp_pct":0.0,"cp_pct":<float>,"opt_pct":<0.05 or 0.10>,'
        '"n_cp_positions":<1,2,or 3>,"cp_max_hold_days":<2,3,or 4>,'
        '"reasoning":"<una frase en español>"}'
    )

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        for fence in ("```json", "```"):
            if raw.startswith(fence):
                raw = raw[len(fence):]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()

        data = json.loads(raw)
        cp   = float(data.get("cp_pct", 0.78))
        opt  = float(data.get("opt_pct", 0.10))
        # Guardar invariantes: LP=0, cp+opt<=0.95, mínimos razonables
        opt  = min(0.10, max(0.05, opt))
        cp   = min(0.88, max(0.40, cp))
        if cp + opt > 0.95:
            cp = 0.95 - opt

        # Override por market predictor (post-AI, hard constraint)
        if prediction is not None and prediction.conviction >= 0.65:
            if prediction.direction == "BEARISH":
                cp = min(cp, 0.55)
            elif prediction.direction == "BULLISH":
                cp = min(cp + 0.05, 0.88)

        level = 1 if cp >= 0.80 else (3 if cp <= 0.55 else 2)

        dec = AllocationDecision(
            lp_pct=0.0,
            cp_pct=round(cp, 2),
            opt_pct=round(opt, 2),
            n_cp_positions=max(1, min(3, int(data.get("n_cp_positions", 2)))),
            cp_max_hold_days=max(2, min(4, int(data.get("cp_max_hold_days", 3)))),
            reasoning=str(data.get("reasoning", "AI allocation.")),
            level=level,
        )
        log.info(
            "AI Allocation → CP=%.0f%% OPT=%.0f%% cash=%.0f%% | %d pos | max %dd | streak=%s | %s",
            dec.cp_pct * 100, dec.opt_pct * 100,
            (1.0 - dec.cp_pct - dec.opt_pct) * 100,
            dec.n_cp_positions, dec.cp_max_hold_days,
            ctx.get("streak", "?"), dec.reasoning,
        )
        return dec

    except Exception as exc:
        log.warning("AI allocation failed (%s) — rule-based fallback", exc)
        return _rule_default(regime, vix, win_rate, recent_pnl)
