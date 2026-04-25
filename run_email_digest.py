"""
Weekly email digest — corre los viernes a las 17:00.

Genera un HTML email con: equity, mejores/peores picks de la semana,
radar top movers, EDGAR alerts, y outlook de la próxima semana.

Uso:
    python run_email_digest.py
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
import sys
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("email_digest")

BASE_DIR    = Path(__file__).parent.resolve()
SIGNALS_DIR = BASE_DIR / "signals"
RECIPIENT   = "nfelix.geo@gmail.com"


def _load_signals() -> dict:
    path = SIGNALS_DIR / "latest.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _section(title: str, body: str) -> str:
    return f"""
<div style="margin-bottom:24px">
  <h2 style="font-size:13px;text-transform:uppercase;letter-spacing:1px;
             color:#f59e0b;border-bottom:1px solid #30363d;padding-bottom:4px;margin-bottom:12px">
    {title}
  </h2>
  {body}
</div>"""


def _build_html(signals: dict, trades: list[dict], equity: float, initial: float) -> str:
    now     = datetime.now()
    pnl     = equity - initial
    pnl_pct = (pnl / initial * 100) if initial else 0
    color   = "#22c55e" if pnl >= 0 else "#ef4444"

    # Equity KPI
    equity_sec = f"""
<div style="display:flex;gap:24px;flex-wrap:wrap;margin-bottom:8px">
  <div style="background:#161b22;border:1px solid #30363d;border-radius:4px;padding:12px 20px">
    <div style="font-size:11px;color:#7d8590;text-transform:uppercase">Equity actual</div>
    <div style="font-size:22px;font-weight:700;font-family:monospace;color:#e6edf3">${equity:,.2f}</div>
  </div>
  <div style="background:#161b22;border:1px solid #30363d;border-radius:4px;padding:12px 20px">
    <div style="font-size:11px;color:#7d8590;text-transform:uppercase">P&amp;L semana</div>
    <div style="font-size:22px;font-weight:700;font-family:monospace;color:{color}">{pnl_pct:+.2f}%</div>
  </div>
  <div style="background:#161b22;border:1px solid #30363d;border-radius:4px;padding:12px 20px">
    <div style="font-size:11px;color:#7d8590;text-transform:uppercase">Trades</div>
    <div style="font-size:22px;font-weight:700;font-family:monospace;color:#e6edf3">{len(trades)}</div>
  </div>
</div>"""

    # Señales LP
    lp_rows = ""
    for s in signals.get("long_term", [])[:4]:
        t   = s.get("ticker", "")
        w   = s.get("weight_target", 0)
        prc = s.get("price", 0)
        sl  = s.get("stop_loss") or 0
        lp_rows += (
            f"<tr><td style='padding:4px 8px;border-bottom:1px solid #21262d'><b>{t}</b></td>"
            f"<td style='padding:4px 8px;border-bottom:1px solid #21262d'>${prc:.2f}</td>"
            f"<td style='padding:4px 8px;border-bottom:1px solid #21262d'>{w:.1%}</td>"
            f"<td style='padding:4px 8px;border-bottom:1px solid #21262d'>${sl:.2f}</td></tr>"
        )
    signals_sec = f"""
<table style="width:100%;border-collapse:collapse;font-size:12px;color:#e6edf3">
  <tr style="color:#7d8590;font-size:11px">
    <th style="text-align:left;padding:4px 8px">Ticker</th>
    <th style="text-align:left;padding:4px 8px">Precio</th>
    <th style="text-align:left;padding:4px 8px">Peso LP</th>
    <th style="text-align:left;padding:4px 8px">Stop</th>
  </tr>
  {lp_rows}
</table>"""

    # Radar
    radar_entries = signals.get("radar", {}).get("entries", [])[:5]
    radar_rows = "".join(
        f"<div style='padding:5px 0;border-bottom:1px solid #21262d;font-size:12px'>"
        f"<b>{e.get('ticker','')}</b> &nbsp; "
        f"<span style='color:{'#22c55e' if e.get('change_1d',0)>=0 else '#ef4444'}'>"
        f"{e.get('change_1d',0):+.1f}%</span> &nbsp; "
        f"<span style='color:#7d8590;font-size:11px'>{e.get('headline','')[:80]}</span>"
        f"</div>"
        for e in radar_entries
    )

    # EDGAR
    edgar_alerts = signals.get("edgar_alerts", [])[:4]
    edgar_rows = "".join(
        f"<div style='margin-bottom:8px;padding:8px;border-left:3px solid "
        f"{'#22c55e' if a.get('sentiment')=='bullish' else '#ef4444' if a.get('sentiment')=='bearish' else '#6b7280'};"
        f"background:#161b22;font-size:12px'>"
        f"<b>{a.get('ticker','')}</b> — {a.get('summary','')[:100]}"
        f"</div>"
        for a in edgar_alerts
    ) if edgar_alerts else "<p style='color:#7d8590;font-size:12px'>Sin eventos EDGAR esta semana.</p>"

    # Macro
    macro    = signals.get("macro", {})
    regime   = macro.get("regime", "?").upper()
    regime_c = "#22c55e" if regime == "BULL" else "#ef4444" if regime == "BEAR" else "#f59e0b"
    macro_sec = (
        f"<p style='font-size:12px;color:#e6edf3'>"
        f"Régimen: <span style='color:{regime_c};font-weight:700'>{regime}</span> &nbsp;|&nbsp; "
        f"{macro.get('regime_reason','')}</p>"
    )

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>body{{font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#0d1117;color:#e6edf3;margin:0;padding:0}}</style>
</head>
<body>
<div style="max-width:600px;margin:0 auto;padding:24px">
  <div style="display:flex;align-items:center;margin-bottom:24px">
    <div>
      <div style="font-family:monospace;font-size:18px;font-weight:700;color:#f59e0b">ALPHA / TERMINAL</div>
      <div style="font-size:11px;color:#7d8590;text-transform:uppercase;letter-spacing:1px">
        Weekly Digest · {now.strftime('%A %d %B %Y')}
      </div>
    </div>
  </div>
  {_section("Equity & Performance", equity_sec)}
  {_section("Posiciones LP Activas", signals_sec)}
  {_section("Radar — Top Movers", radar_rows or "<p style='color:#7d8590;font-size:12px'>Sin datos de radar.</p>")}
  {_section("EDGAR 8-K Alerts", edgar_rows)}
  {_section("Contexto Macro", macro_sec)}
  <p style="font-size:10px;color:#7d8590;border-top:1px solid #30363d;padding-top:12px;margin-top:24px">
    Alpha Terminal · Paper Trading · {now.strftime('%Y-%m-%d %H:%M')}
  </p>
</div>
</body></html>"""
    return html


def send_email(subject: str, html_body: str, recipient: str) -> None:
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER", recipient)
    smtp_pass = os.environ.get("SMTP_PASS", "")

    if not smtp_pass:
        logger.warning("SMTP_PASS no configurado — email no enviado. Configura en .env")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = smtp_user
    msg["To"]      = recipient
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.ehlo()
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, recipient, msg.as_bytes())
    logger.info("Email enviado a %s", recipient)


def main() -> None:
    load_dotenv(BASE_DIR / ".env")

    signals = _load_signals()
    if not signals:
        logger.warning("No hay signals/latest.json — abortando digest.")
        return

    # Trades de la semana
    trades: list[dict] = []
    try:
        from alpha_agent.analytics.trade_db import get_trades
        trades = get_trades(since=datetime.now() - timedelta(days=7), limit=200)
    except Exception as exc:
        logger.warning("trade_db no disponible: %s", exc)

    # Equity actual
    equity  = 1600.0
    initial = 1600.0
    try:
        from alpaca.trading.client import TradingClient
        client = TradingClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"], paper=True)
        acct   = client.get_account()
        equity = float(acct.equity)
    except Exception as exc:
        logger.warning("No se pudo leer equity: %s", exc)

    html    = _build_html(signals, trades, equity, initial)
    subject = f"Alpha Terminal · Weekly Digest · {datetime.now().strftime('%d %b %Y')}"

    try:
        send_email(subject, html, RECIPIENT)
    except Exception as exc:
        logger.error("Error enviando email: %s", exc)


if __name__ == "__main__":
    main()
