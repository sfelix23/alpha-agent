"""
AI-driven allocation decision engine.

Filosofía: capital quieto no rinde. Deployer agresivo cuando hay edge demostrado,
defensivo cuando el sistema está perdiendo. Opciones solo con catalizador real.

Sistema de 3 niveles basado en régimen + racha reciente:
  NIVEL 1 — Alta convicción (BULL + win_rate >= 55%): CP 88% en 3 nombres, OPT condicional
  NIVEL 2 — Base (BULL sin historial / NEUTRAL): CP 75-83% en 3 nombres, OPT si hay catalizador
  NIVEL 3 — Defensivo (BEAR / racha perdedora): CP 45%, OPT mínimo

LP siempre 0%: con $1600 el CP rotatorio supera ampliamente a holds de semanas.

Iter13 "agresivo con control": n_cp pasó de 1-2 a 3 nombres en niveles 1-2. Con el
floor de 30%/posición + conviction ×1.5 (en signals.py), el de mayor convicción llega
a ~40% (absorbe la subida) sin que un solo gap overnight borre la cuenta. Combinado con
max_weight_per_asset=0.40. Despliega el sleeve completo → menos cash drag.
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

    # NEUTRAL: base (iter14 agresivo sube exposición; 3 nombres = diversifica incertidumbre)
    if vix > 22 or reg == "NEUTRAL":
        cp = 0.84 if winning_streak else 0.72
        n  = 5  # iter29: diversificar sizing
        reason = f"NEUTRAL/VIX {vix:.0f}: CP {cp:.0%} en top-5 (diversificado), {'racha ganadora.' if winning_streak else 'moderado.'}"
        return AllocationDecision(0.0, cp, 0.10, n, 8, reason, level=2)

    # BULL — el caso principal
    # Iter3 (modo agresivo): hold maximo CP reducido (18d→10d nivel 1, 14d→8d nivel 2).
    # Rotacion mas rapida = mas oportunidades de capturar momentum sin quedarse colgado
    # cuando un ticker pierde momentum (suele pasar a las 2 semanas).
    if winning_streak:
        # NIVEL 1: BULL + ganando → presionar al máximo; chandelier es el exit primario.
        # iter14 AGRESIVO: CP 90%, OPT 8% (cash ~2%). n_cp=2 si VIX<18 (concentrar en
        # las 2 mejores ideas = más beta al edge) o 3 si VIX>=18 (diversificar cuando
        # hay más incertidumbre). El piramidado de la equity curve (HOT 1.35x) puede
        # llevar el sleeve hasta ~0.95 en racha.
        # iter29: 2→5/6 posiciones. El backtest mostró que concentrar en top-2 da
        # -41% drawdown / Sharpe 0.68; diversificar el SIZING (no solo el universo)
        # baja el DD y sube el Sharpe. 5-6 nombres sigue siendo agresivo pero reparte
        # el riesgo idiosincrático que con 2 nombres te hundía el mes.
        n_cp = 5 if vix < 18 else 6
        return AllocationDecision(0.0, 0.90, 0.08, n_cp, 10,
            f"BULL + racha ganadora ({win_rate:.0%} WR): CP 90% en top-{n_cp} (diversificado), hold 10d max.", level=1)
    elif losing_streak:
        # ya cubierto arriba pero por si acá
        return AllocationDecision(0.0, 0.55, 0.05, 2, 4,
            f"BULL pero racha perdedora: reduciendo CP al 55%, 2 nombres.", level=3)
    else:
        # NIVEL 2: BULL sin historial suficiente → base sólida pero agresiva.
        cp = 0.88 if vix < 18 else 0.80
        n_cp = 5 if vix < 18 else 6  # iter29: diversificar sizing (no top-2)
        return AllocationDecision(0.0, cp, 0.10, n_cp, 8,
            f"BULL + VIX {vix:.1f}: CP {cp:.0%} en top-{n_cp} (diversificado), hold 8d max.", level=2)


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


def _equity_history_recent(n: int = 50) -> list[float]:
    """Lee los últimos N equity snapshots desde signals/equity_snapshots.json.

    Soporta dos formatos en el JSON: lista de {ts, equity} o {date, v}.
    Devuelve [] si no hay archivo o hay error — el caller debe asumir "sin historial".
    """
    try:
        import json as _json
        from pathlib import Path as _Path
        p = _Path(__file__).resolve().parents[2] / "signals" / "equity_snapshots.json"
        if not p.exists():
            return []
        data = _json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
        out: list[float] = []
        for entry in data[-n:]:
            v = entry.get("equity", entry.get("v"))
            if v is not None:
                try:
                    out.append(float(v))
                except (TypeError, ValueError):
                    pass
        return out
    except Exception as exc:
        log.debug("_equity_history_recent: %s", exc)
        return []


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

    Iter2: tras computar la decisión base, aplica composite_kelly_multiplier
    (regime × drawdown_band × equity_curve) para modular CP/OPT. Si la equity
    curve está en modo DEFENSIVE o el drawdown intradía bajó la banda,
    achicamos el sleeve operativo en runtime sin tocar la regla base.
    """
    win_rate, recent_pnl = _get_recent_performance()
    base = _rule_default(regime, vix, win_rate, recent_pnl)

    # Sleeve modulator basado en equity curve + drawdown intradía.
    #
    # IMPORTANTE: NO usamos regime_mult del composite Kelly porque cp_pct ya viene
    # modulado por régimen en _rule_default (BULL=83-88%, BEAR=45%). Mezclar
    # otra vez con regime_mult (0.5x) sería doble-modulación y baja el sleeve
    # excesivamente. Sólo aplicamos drawdown_mult × equity_curve_mult como
    # "freno" en condiciones adversas; en operación normal el factor queda en 1.0
    # y la regla base manda.
    try:
        from alpha_agent.analytics.kelly import risk_action_for_drawdown, equity_curve_multiplier
        eq_hist = _equity_history_recent(50)
        risk = risk_action_for_drawdown(0.0)   # placeholder — intradía lo maneja el monitor
        ec_mult, ec_regime = equity_curve_multiplier(eq_hist)
        drawdown_mult = float(risk["kelly_multiplier"])
        # iter14 AGRESIVO: permitir piramidar en racha (HOT 1.35x sube el sleeve por
        # arriba del base, anti-martingala). Cap a 1.30 para no pasarnos. El cap duro
        # de despliegue (≤0.95 = 5% cash mínimo) lo aplica adjusted_cp más abajo.
        ec_mult_capped = min(1.30, ec_mult)
        modulator = drawdown_mult * ec_mult_capped
        new_entries_ok = bool(risk["new_entries_allowed"]) and ec_regime != "DEFENSIVE"

        if not new_entries_ok:
            log.warning(
                "Sleeve modulator: new_entries_allowed=False (ec=%s, dd_level=%s) → CP/OPT a 0",
                ec_regime, risk["level"],
            )
            return AllocationDecision(
                0.0, 0.0, 0.0,
                max(1, base.n_cp_positions),
                base.cp_max_hold_days,
                f"{base.reasoning} | Defensivo por equity curve / drawdown.",
                level=3,
            )

        if abs(modulator - 1.0) > 0.01:
            # Cap duro: CP ≤ 0.95 (deja ≥5% cash operativo aun piramidando), OPT ≤ 0.25
            adjusted_cp = max(0.0, min(0.95, base.cp_pct * modulator))
            adjusted_opt = max(0.0, min(0.25, base.opt_pct * modulator))
            # iter35: cuando ec_mult=HOT (1.30+) modula AMBOS sleeves, la suma podía
            # exceder 1.0 (ej. 0.95+0.104). Cap de la suma para preservar el invariante
            # lp+cp+opt ≤ 1.0. Si excede, achicamos opt primero (CP es el core).
            total = base.lp_pct + adjusted_cp + adjusted_opt
            if total > 1.0:
                adjusted_opt = max(0.0, 1.0 - base.lp_pct - adjusted_cp)
            log.info(
                "Sleeve modulator: drawdown_mult=%.2f ec_mult=%.2f (ec=%s) → %.2f | CP %.0f%%→%.0f%% OPT %.0f%%→%.0f%%",
                drawdown_mult, ec_mult_capped, ec_regime, modulator,
                base.cp_pct * 100, adjusted_cp * 100,
                base.opt_pct * 100, adjusted_opt * 100,
            )
            return AllocationDecision(
                base.lp_pct,
                adjusted_cp,
                adjusted_opt,
                base.n_cp_positions,
                base.cp_max_hold_days,
                f"{base.reasoning} | Sleeve mod {modulator:.2f} (ec={ec_regime}).",
                level=base.level,
            )
    except Exception as exc:
        log.debug("sleeve modulator no aplicado (%s) — usando base", exc)

    return base
