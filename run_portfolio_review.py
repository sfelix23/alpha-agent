"""
Portfolio Review Agent — corre los domingos.

Lee trades.db, analiza rendimiento de la semana, detecta qué funcionó/falló,
y manda recomendaciones por WhatsApp via Claude Sonnet.

Uso:
    python run_portfolio_review.py
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("portfolio_review")

BASE_DIR = Path(__file__).parent.resolve()


def _build_trades_summary(trades: list[dict]) -> str:
    if not trades:
        return "Sin trades registrados esta semana."
    lines = []
    for t in trades:
        lines.append(
            f"  {t.get('date','?')} | {t.get('side','?')} {t.get('ticker','?')} "
            f"qty={t.get('qty',0):.2f} @ ${t.get('price',0):.2f} "
            f"notional=${t.get('notional',0):.0f} sleeve={t.get('sleeve','?')} "
            f"status={t.get('status','?')} regime={t.get('regime','?')}"
        )
    return "\n".join(lines)


def _analyze_with_claude(trades_text: str, equity_now: float, equity_week_ago: float) -> str:
    """Claude Sonnet analiza los trades de la semana y da recomendaciones."""
    try:
        import anthropic
        client = anthropic.Anthropic()
        pnl_pct = ((equity_now - equity_week_ago) / equity_week_ago * 100) if equity_week_ago else 0
        prompt = f"""Eres un portfolio manager cuantitativo revisando la semana de trading.

EQUITY: ${equity_week_ago:,.0f} → ${equity_now:,.0f} ({pnl_pct:+.1f}% en 7 días)

TRADES DE LA SEMANA:
{trades_text}

Analiza brevemente (máx 400 palabras):
1. ¿Qué funcionó esta semana y por qué?
2. ¿Qué no funcionó? ¿Hay un patrón?
3. ¿Ajustes concretos para la próxima semana? (ej: reducir size en X sector, evitar entradas en régimen Y)
4. Oportunidades que el sistema podría haber capturado mejor.

Sé directo y cuantitativo. Sin bullet points de relleno."""
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as exc:
        logger.warning("Claude review fallido: %s", exc)
        return "(Análisis IA no disponible esta semana)"


def main() -> None:
    load_dotenv(BASE_DIR / ".env")

    from alpha_agent.analytics.trade_db import get_trades
    from alpha_agent.notifications import send_whatsapp

    now = datetime.now()
    week_ago = now - timedelta(days=7)

    trades = get_trades(since=week_ago, limit=200)
    logger.info("Trades de la semana: %d", len(trades))

    # Intentar leer equity actual y de hace 7 días desde Alpaca
    equity_now   = 0.0
    equity_7d    = 0.0
    try:
        from alpaca.trading.client import TradingClient
        import os
        client = TradingClient(
            os.environ["ALPACA_API_KEY"],
            os.environ["ALPACA_SECRET_KEY"],
            paper=True,
        )
        acct = client.get_account()
        equity_now = float(acct.equity)
        equity_7d  = float(acct.last_equity) if hasattr(acct, "last_equity") else equity_now
    except Exception as exc:
        logger.warning("No se pudo leer equity de Alpaca: %s", exc)
        equity_now = 1600.0
        equity_7d  = 1600.0

    trades_text = _build_trades_summary(trades)
    analysis    = _analyze_with_claude(trades_text, equity_now, equity_7d)

    pnl      = equity_now - equity_7d
    pnl_pct  = (pnl / equity_7d * 100) if equity_7d else 0
    emoji    = "📈" if pnl >= 0 else "📉"

    msg = (
        f"🤖 *PORTFOLIO REVIEW SEMANAL* · {now.strftime('%d/%m/%Y')}\n\n"
        f"{emoji} Equity: ${equity_now:,.0f} ({pnl_pct:+.1f}% vs semana pasada)\n"
        f"Trades ejecutados: {len(trades)}\n\n"
        f"*Análisis:*\n{analysis}"
    )

    send_whatsapp(msg)
    logger.info("Portfolio review enviado.")


if __name__ == "__main__":
    main()
