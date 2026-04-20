"""
Dashboard HTML estático — genera D:\Agente\dashboard.html

Muestra:
    - Equity actual vs capital inicial
    - Posiciones abiertas con P&L
    - Últimas señales del analyst (latest.json)
    - Historial de equity (Alpaca portfolio history)

Uso:
    python run_dashboard.py              # genera dashboard.html y lo abre
    python run_dashboard.py --no-open    # solo genera, no abre el browser
    python run_dashboard.py --watch      # genera + re-genera cada 5 min
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR     = Path(__file__).parent.resolve()
SIGNALS_PATH = BASE_DIR / "signals" / "latest.json"
OUT_PATH     = BASE_DIR / "dashboard.html"

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logger = logging.getLogger("dashboard")
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")


# ── helpers ──────────────────────────────────────────────────────────────────

def _pnl_color(v: float) -> str:
    return "#2ecc71" if v >= 0 else "#e74c3c"


def _regime_color(regime: str) -> str:
    return {"bull": "#2ecc71", "bear": "#e74c3c", "sideways": "#f39c12"}.get(regime.lower(), "#aaa")


def _fmt_pct(v: float) -> str:
    return f"{v:+.2f}%"


def _fmt_usd(v: float) -> str:
    return f"${v:,.2f}"


# ── secciones HTML ────────────────────────────────────────────────────────────

def _build_equity_section(equity: float, initial: float, history: list[dict]) -> str:
    total_pnl     = equity - initial
    total_pnl_pct = (total_pnl / initial * 100) if initial else 0
    color         = _pnl_color(total_pnl)

    # Mini sparkline con SVG
    spark = ""
    if len(history) >= 2:
        vals  = [h["equity"] for h in history[-60:]]
        mn, mx = min(vals), max(vals)
        rng   = mx - mn if mx != mn else 1
        w, h  = 300, 60
        pts   = " ".join(
            f"{int(i / (len(vals) - 1) * w)},{int(h - (v - mn) / rng * h)}"
            for i, v in enumerate(vals)
        )
        spark = (
            f'<svg width="{w}" height="{h}" style="display:block;margin:8px auto">'
            f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2"/>'
            f"</svg>"
        )

    return f"""
<div class="card">
  <h2>Equity</h2>
  <div class="big-num" style="color:{color}">{_fmt_usd(equity)}</div>
  <div class="sub">Capital inicial: {_fmt_usd(initial)} &nbsp;|&nbsp;
       P&amp;L total: <span style="color:{color}">{_fmt_usd(total_pnl)} ({_fmt_pct(total_pnl_pct)})</span>
  </div>
  {spark}
</div>"""


def _build_positions_section(positions: list) -> str:
    if not positions:
        return '<div class="card"><h2>Posiciones</h2><p class="sub">Sin posiciones abiertas.</p></div>'

    rows = ""
    for p in positions:
        pnl     = p.unrealized_pl
        pnl_pct = (pnl / (p.avg_price * p.qty) * 100) if p.avg_price and p.qty else 0
        c       = _pnl_color(pnl)
        rows += f"""
  <tr>
    <td><b>{p.ticker}</b></td>
    <td>{p.qty:.4f}</td>
    <td>{_fmt_usd(p.avg_price)}</td>
    <td>{_fmt_usd(p.market_value)}</td>
    <td style="color:{c}">{_fmt_usd(pnl)}</td>
    <td style="color:{c}">{_fmt_pct(pnl_pct)}</td>
  </tr>"""

    return f"""
<div class="card">
  <h2>Posiciones abiertas ({len(positions)})</h2>
  <table>
    <thead><tr><th>Ticker</th><th>Qty</th><th>Entrada</th><th>Valor</th><th>P&amp;L $</th><th>P&amp;L %</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>"""


def _build_signals_section(signals_data: dict) -> str:
    if not signals_data:
        return '<div class="card"><h2>Últimas señales</h2><p class="sub">Sin señales.</p></div>'

    macro   = signals_data.get("macro", {})
    regime  = macro.get("regime", "?")
    rc      = _regime_color(regime)
    vix     = macro.get("prices", {}).get("vix", 0)
    gen_at  = signals_data.get("generated_at", "?")
    capital = signals_data.get("capital_usd", 0)

    rows = ""
    for bucket, label in [("long_term", "LP"), ("short_term", "CP")]:
        for s in signals_data.get(bucket, []):
            thesis = s.get("thesis", {})
            risk   = thesis.get("risk", {})
            q      = thesis.get("quant", {})
            conv   = thesis.get("conviction", "?")
            conv_c = {"ALTA": "#2ecc71", "MEDIA": "#f39c12", "BAJA": "#e74c3c"}.get(conv, "#aaa")
            rows += f"""
  <tr>
    <td><b>{s['ticker']}</b></td>
    <td>{label}</td>
    <td style="color:{conv_c}">{conv}</td>
    <td>{_fmt_usd(s.get('price', 0))}</td>
    <td>{_fmt_usd(risk.get('dollars_allocated', 0))}</td>
    <td>{_fmt_pct(q.get('sharpe', 0) * 100) if False else f"{q.get('sharpe', 0):.2f}"}</td>
    <td>{_fmt_pct(q.get('alpha_jensen', 0) * 100)}</td>
    <td>{_fmt_usd(s.get('stop_loss') or 0)}</td>
    <td>{_fmt_usd(s.get('take_profit') or 0)}</td>
  </tr>"""

    return f"""
<div class="card">
  <h2>Señales — {gen_at[:16]}</h2>
  <div class="sub">
    Capital: {_fmt_usd(capital)} &nbsp;|&nbsp;
    Régimen: <span style="color:{rc};font-weight:bold">{regime.upper()}</span> &nbsp;|&nbsp;
    VIX: {vix:.1f}
  </div>
  <table>
    <thead><tr>
      <th>Ticker</th><th>Sleeve</th><th>Convicción</th><th>Precio</th>
      <th>Asignado</th><th>Sharpe</th><th>Alfa α</th><th>SL</th><th>TP</th>
    </tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>"""


# ── HTML completo ─────────────────────────────────────────────────────────────

_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Segoe UI', sans-serif; background: #0d1117; color: #c9d1d9; padding: 20px; }
h1 { color: #58a6ff; margin-bottom: 16px; }
h2 { color: #8b949e; font-size: 0.9rem; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 12px; }
.grid { display: flex; flex-wrap: wrap; gap: 16px; }
.card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px;
        flex: 1 1 420px; min-width: 300px; }
.big-num { font-size: 2.4rem; font-weight: 700; margin: 4px 0; }
.sub { font-size: 0.82rem; color: #8b949e; margin-top: 4px; }
table { width: 100%; border-collapse: collapse; font-size: 0.83rem; margin-top: 8px; }
th { color: #8b949e; text-align: left; padding: 4px 8px; border-bottom: 1px solid #30363d; }
td { padding: 5px 8px; border-bottom: 1px solid #21262d; }
tr:hover td { background: #1f2937; }
.refresh { font-size: 0.75rem; color: #484f58; margin-top: 20px; }
"""


def build_html(
    equity: float,
    initial: float,
    history: list[dict],
    positions: list,
    signals_data: dict,
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="refresh" content="300">
  <title>Alpha Dashboard</title>
  <style>{_CSS}</style>
</head>
<body>
  <h1>Alpha Dashboard</h1>
  <div class="grid">
    {_build_equity_section(equity, initial, history)}
    {_build_positions_section(positions)}
    {_build_signals_section(signals_data)}
  </div>
  <p class="refresh">Última actualización: {now} · Auto-refresh cada 5 min</p>
</body>
</html>"""


# ── main ──────────────────────────────────────────────────────────────────────

def generate() -> None:
    from trader_agent.brokers.alpaca_broker import AlpacaBroker
    from alpha_agent.config import PARAMS

    broker = AlpacaBroker(paper=True)

    try:
        equity    = broker.get_equity()
        positions = broker.get_positions()
    except Exception as e:
        logger.error("Error Alpaca: %s", e)
        equity, positions = PARAMS.paper_capital_usd, []

    # Historial de equity (Alpaca portfolio history, últimos 30 días)
    history: list[dict] = []
    try:
        ph = broker._trading.get_portfolio_history(period="1M", timeframe="1D")
        if ph and ph.equity:
            timestamps = ph.timestamp or []
            history = [
                {"ts": t, "equity": float(e)}
                for t, e in zip(timestamps, ph.equity)
                if e is not None
            ]
    except Exception as e:
        logger.debug("No se pudo obtener portfolio history: %s", e)

    signals_data: dict = {}
    if SIGNALS_PATH.exists():
        try:
            signals_data = json.loads(SIGNALS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass

    initial = signals_data.get("capital_usd", PARAMS.paper_capital_usd)

    html = build_html(equity, initial, history, positions, signals_data)
    OUT_PATH.write_text(html, encoding="utf-8")
    logger.info("Dashboard generado → %s", OUT_PATH)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--watch",   action="store_true", help="Re-genera cada 5 min")
    args = parser.parse_args()

    load_dotenv(BASE_DIR / ".env")

    generate()

    if not args.no_open:
        import webbrowser
        webbrowser.open(OUT_PATH.as_uri())

    if args.watch:
        logger.info("Modo watch activo (Ctrl+C para detener).")
        while True:
            time.sleep(300)
            generate()


if __name__ == "__main__":
    main()
