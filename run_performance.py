"""
Reporte semanal de performance — corre viernes 17:00 ART.

Calcula y manda por Telegram + WhatsApp:
  - P&L total realizado (trade_db) y no realizado (Alpaca)
  - Win rate, Sharpe semanal, avg hold days
  - Alpha vs SPY (semana y mes)
  - Mejor y peor trade de la semana
  - Estado actual del portfolio

Sin argumentos — siempre envía el reporte.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.platform == "win32" and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s — %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("performance")


def _spy_returns(days: int) -> float | None:
    try:
        import yfinance as yf
        df = yf.download("SPY", period=f"{days + 5}d", interval="1d",
                         progress=False, auto_adjust=True)
        if df is None or len(df) < 2:
            return None
        close = df["Close"].squeeze()
        return float((close.iloc[-1] - close.iloc[-days]) / close.iloc[-days] * 100)
    except Exception:
        return None


def _sharpe(pnl_list: list[float], risk_free_daily: float = 4.5 / 252 / 100) -> float | None:
    if len(pnl_list) < 3:
        return None
    import statistics
    mean = statistics.mean(pnl_list)
    std  = statistics.stdev(pnl_list)
    if std == 0:
        return None
    return round((mean - risk_free_daily) / std * (252 ** 0.5), 2)


def main() -> None:
    log.info("=== PERFORMANCE REPORT ===")

    from alpha_agent.analytics.trade_db import get_trades, get_summary
    from alpha_agent.notifications import send_notification

    now   = datetime.now()
    week_ago  = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    month_ago = (now - timedelta(days=30)).strftime("%Y-%m-%d")

    all_closed = [t for t in get_trades(limit=500)
                  if t.get("side") == "BUY" and t.get("pnl_usd") is not None]
    week_closed  = [t for t in all_closed if (t.get("date") or "") >= week_ago]
    month_closed = [t for t in all_closed if (t.get("date") or "") >= month_ago]

    summary = get_summary()

    # ── P&L no realizado (Alpaca) ───────────────────────────────────────────
    unrealized   = 0.0
    equity_now   = 0.0
    n_open       = 0
    try:
        from trader_agent.brokers.alpaca_broker import AlpacaBroker
        broker     = AlpacaBroker(paper=True)
        positions  = broker.get_positions()
        equity_now = broker.get_equity()
        unrealized = sum(p.unrealized_pl for p in positions)
        n_open     = len(positions)
    except Exception as e:
        log.warning("Alpaca no disponible: %s", e)

    # ── Métricas semanales ──────────────────────────────────────────────────
    week_pnl    = sum(t.get("pnl_usd", 0) or 0 for t in week_closed)
    week_pnl_pcts = [t.get("pnl_pct", 0) or 0 for t in week_closed if t.get("pnl_pct") is not None]
    week_wins   = sum(1 for t in week_closed if (t.get("pnl_usd") or 0) > 0)
    week_wr     = week_wins / len(week_closed) if week_closed else None
    week_sharpe = _sharpe(week_pnl_pcts)

    month_pnl   = sum(t.get("pnl_usd", 0) or 0 for t in month_closed)

    # ── Alpha vs SPY ───────────────────────────────────────────────────────
    capital_base = 1600.0
    try:
        import json
        sig = json.loads((Path("signals") / "latest.json").read_text(encoding="utf-8"))
        capital_base = float(sig.get("capital_usd", 1600))
    except Exception:
        pass

    week_return_pct  = week_pnl / capital_base * 100 if capital_base > 0 else 0.0
    month_return_pct = month_pnl / capital_base * 100 if capital_base > 0 else 0.0

    spy_1w  = _spy_returns(5)
    spy_1m  = _spy_returns(21)
    alpha_1w = round(week_return_pct - spy_1w, 2)  if spy_1w  is not None else None
    alpha_1m = round(month_return_pct - spy_1m, 2) if spy_1m  is not None else None

    # ── Mejor y peor trade de la semana ────────────────────────────────────
    best  = max(week_closed, key=lambda t: t.get("pnl_usd") or 0) if week_closed else None
    worst = min(week_closed, key=lambda t: t.get("pnl_usd") or 0) if week_closed else None

    # ── Construir mensaje ──────────────────────────────────────────────────
    date_str = now.strftime("%d-%b-%Y")
    lines = [f"📈 *REPORTE SEMANAL* · {date_str}"]
    lines.append("")

    # P&L
    pnl_icon = "🟢" if week_pnl >= 0 else "🔴"
    lines.append(f"{pnl_icon} *P&L semana (realizado):* ${week_pnl:+.2f} ({week_return_pct:+.2f}%)")
    lines.append(f"   P&L mes: ${month_pnl:+.2f} ({month_return_pct:+.2f}%)")
    lines.append(f"   No realizado: ${unrealized:+.2f} | Equity: ${equity_now:.2f}")

    # Alpha vs SPY
    lines.append("")
    lines.append("*Alpha vs SPY:*")
    spy_1w_str  = f"{spy_1w:+.2f}%"  if spy_1w  is not None else "N/A"
    spy_1m_str  = f"{spy_1m:+.2f}%"  if spy_1m  is not None else "N/A"
    a1w_str     = (f"{alpha_1w:+.2f}%" if alpha_1w is not None else "N/A")
    a1m_str     = (f"{alpha_1m:+.2f}%" if alpha_1m is not None else "N/A")
    a1w_icon    = "✅" if (alpha_1w or 0) >= 0 else "⚠️"
    a1m_icon    = "✅" if (alpha_1m or 0) >= 0 else "⚠️"
    lines.append(f"  1 semana: Alpha {a1w_str} {a1w_icon} (SPY {spy_1w_str})")
    lines.append(f"  1 mes:    Alpha {a1m_str} {a1m_icon} (SPY {spy_1m_str})")

    # Estadísticas
    lines.append("")
    lines.append("*Estadísticas (semana):*")
    if week_closed:
        lines.append(f"  Trades cerrados: {len(week_closed)}")
        lines.append(f"  Win rate: {week_wr:.0%}" if week_wr is not None else "  Win rate: N/A")
        if week_sharpe is not None:
            lines.append(f"  Sharpe (anualizado): {week_sharpe:.2f}")
        avg_hold = sum(t.get("hold_days") or 0 for t in week_closed) / len(week_closed)
        lines.append(f"  Hold medio: {avg_hold:.1f} días")
    else:
        lines.append("  Sin trades cerrados esta semana")

    # All-time
    if summary.get("closed_trades"):
        lines.append("")
        lines.append("*All-time:*")
        lines.append(f"  Trades: {summary['closed_trades']} | WR: {summary['win_rate']:.0%}")
        lines.append(f"  P&L total: ${summary['total_pnl_usd']:+.2f}")
        lines.append(f"  Avg hold: {summary['avg_hold_days']:.1f}d")

    # Mejor / peor
    if best or worst:
        lines.append("")
        if best:
            lines.append(f"🏆 Mejor: {best['ticker']} ${best['pnl_usd']:+.2f} ({best.get('pnl_pct', 0):+.1f}%)")
        if worst and worst != best:
            lines.append(f"💀 Peor: {worst['ticker']} ${worst['pnl_usd']:+.2f} ({worst.get('pnl_pct', 0):+.1f}%)")

    # Posiciones abiertas
    lines.append("")
    lines.append(f"*Cartera actual:* {n_open} posición(es) abiertas")

    msg = "\n".join(lines)
    log.info("Enviando reporte (%d chars)...", len(msg))
    send_notification(msg)
    log.info("=== PERFORMANCE REPORT OK ===")


if __name__ == "__main__":
    main()
