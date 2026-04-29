"""
Expected Value calculator para el Risk Auditor del Swarm.

EV = P(win) * avg_win_$ - P(loss) * avg_loss_$

Con ≥10 trades históricos DT cerrados: usa estadísticas reales.
Con <10 trades: valores teóricos calibrados para setups ORB/gap (win_rate 42%).

También calcula la fracción de Kelly (half-Kelly) para validar el sizing.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_MIN_HISTORY = 10


def compute_ev(
    notional: float,
    sl_pct: float,
    tp1_pct: float,
    tp2_pct: float,
    sleeve: str = "DT",
) -> dict:
    """
    Args:
        notional:  capital desplegado en el trade (USD)
        sl_pct:    stop loss como fracción (0.015 = 1.5%)
        tp1_pct:   take profit 1 como fracción (0.03 = 3%)
        tp2_pct:   take profit 2 como fracción (0.07 = 7%)
        sleeve:    "DT" | "CP" para filtrar trade_db

    Returns:
        dict con ev, win_rate, avg_win, avg_loss, source, kelly_fraction, ev_positive
    """
    closed: list[dict] = []
    try:
        from alpha_agent.analytics.trade_db import get_trades
        trades = get_trades(limit=300)
        closed = [
            t for t in trades
            if t.get("sleeve") == sleeve
            and t.get("side") in ("BUY", "SELL")
            and t.get("closed_at")
            and t.get("pnl_usd") is not None
        ]
    except Exception as e:
        log.debug("EV: trade_db no disponible: %s", e)

    if len(closed) >= _MIN_HISTORY:
        wins   = [t["pnl_usd"] for t in closed if t["pnl_usd"] > 0]
        losses = [abs(t["pnl_usd"]) for t in closed if t["pnl_usd"] <= 0]
        win_rate = len(wins) / len(closed)
        avg_win  = sum(wins)   / max(len(wins), 1)
        avg_loss = sum(losses) / max(len(losses), 1)
        source   = f"histórico ({len(closed)} trades)"
    else:
        # Dual bracket: la mitad de qty sale en TP1, la otra mitad en TP2
        # Ganancia promedio esperada: media de TP1 y TP2, ajustada por fill parcial
        avg_win  = notional * ((tp1_pct + tp2_pct) / 2) * 0.85  # 15% slippage/comisiones
        avg_loss = notional * sl_pct * 1.05                       # 5% slippage adverso
        win_rate = 0.42                                            # calibrado en backtests ORB
        source   = f"teórico (solo {len(closed)} trades)"

    ev = win_rate * avg_win - (1 - win_rate) * avg_loss

    # Fracción de Kelly: f* = (b*p - q) / b  con half-Kelly
    kelly_f = 0.0
    if avg_loss > 0 and avg_win > 0:
        b = avg_win / avg_loss
        p = win_rate
        q = 1 - p
        full_kelly = (b * p - q) / b
        kelly_f = max(0.0, min(1.0, full_kelly * 0.5))

    result = {
        "ev":             round(ev, 2),
        "win_rate":       round(win_rate, 3),
        "avg_win":        round(avg_win, 2),
        "avg_loss":       round(avg_loss, 2),
        "source":         source,
        "kelly_fraction": round(kelly_f, 3),
        "ev_positive":    ev > 0,
    }
    log.debug("EV[%s]: %s", sleeve, result)
    return result
