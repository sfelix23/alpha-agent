"""
Reporte de cierre de mercado (End-of-Day).

Corre a las 17:05 ART (5 min después del cierre NYSE).
Manda WhatsApp con:
  - P&L del día por posición
  - Equity total vs ayer
  - Posiciones cerca del stop (warning)
  - Top mover del universo
  - Resumen del día en 2 líneas (Claude si disponible)
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent.resolve()

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("eod")


def main() -> None:
    load_dotenv(BASE_DIR / ".env")

    from trader_agent.brokers.alpaca_broker import AlpacaBroker
    from alpha_agent.notifications.whatsapp import send_whatsapp
    from alpha_agent.config import PARAMS

    broker = AlpacaBroker(paper=True)

    try:
        equity    = broker.get_equity()
        positions = broker.get_positions()
    except Exception as e:
        log.error("Error Alpaca: %s", e)
        return

    # Historial para calcular P&L del dia
    try:
        ph = broker._trading.get_portfolio_history(period="2D", timeframe="1D")
        equities = [e for e in (ph.equity or []) if e is not None]
        yesterday_equity = float(equities[-2]) if len(equities) >= 2 else equity
    except Exception:
        yesterday_equity = equity

    day_pnl     = equity - yesterday_equity
    day_pnl_pct = (day_pnl / yesterday_equity * 100) if yesterday_equity else 0
    arrow       = "+" if day_pnl >= 0 else ""

    lines = [
        f"*CIERRE {datetime.now().strftime('%d/%b').upper()}*",
        f"Equity: ${equity:,.0f} | Dia: {arrow}{day_pnl_pct:.2f}% ({arrow}${day_pnl:,.0f})",
        "",
    ]

    warnings = []
    for p in positions:
        pnl     = p.unrealized_pl
        pnl_pct = (pnl / (p.avg_price * p.qty) * 100) if p.avg_price and p.qty else 0
        arrow2  = "+" if pnl >= 0 else ""
        lines.append(f"  {p.ticker}: {arrow2}{pnl_pct:.1f}% (${pnl:+,.0f})")

        # Warning si está cerca del kill switch por posición (-5%)
        if pnl_pct < -4.5:
            warnings.append(f"ATENCION {p.ticker} en {pnl_pct:.1f}% — revisar manana")

    if warnings:
        lines.append("")
        lines.extend(["" + w for w in warnings])

    # Claude summary (si disponible)
    import os
    if os.getenv("ANTHROPIC_API_KEY"):
        try:
            import anthropic
            client = anthropic.Anthropic()
            pos_summary = ", ".join(
                f"{p.ticker} {((p.unrealized_pl/(p.avg_price*p.qty))*100):+.1f}%"
                for p in positions
            )
            msg = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=80,
                messages=[{"role": "user", "content":
                    f"Portfolio EOD: {pos_summary}. Equity cambio {day_pnl_pct:+.2f}% hoy. "
                    f"Dame 1 oracion sobre que vigilar manana. Espanol, directo, sin emojis."}],
            )
            lines.append("")
            lines.append(msg.content[0].text.strip())
        except Exception:
            pass

    message = "\n".join(lines)
    log.info("Enviando EOD report:\n%s", message)
    send_whatsapp(message)


if __name__ == "__main__":
    main()
