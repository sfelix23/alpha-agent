"""
Dashboard HTML interactivo — genera docs/index.html (GitHub Pages).

Incluye:
  - Equity curve con Chart.js (interactiva, zoom, hover)
  - Portfolio allocation pie chart
  - Sector rotation bar chart
  - Posiciones con barras de P&L
  - Últimas señales con conviction y tesis
  - Métricas de riesgo (Sharpe, DD máximo, VIX)
  - Auto-refresh cada 5 min
  - Diseño dark mode, mobile-friendly

Uso:
  python run_dashboard.py              # genera + abre browser
  python run_dashboard.py --no-open   # solo genera (para GitHub Actions)
  python run_dashboard.py --watch     # genera + re-genera cada 5 min
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
DOCS_DIR     = BASE_DIR / "docs"
OUT_PATH     = DOCS_DIR / "index.html"

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logger = logging.getLogger("dashboard")
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")


# ── helpers ───────────────────────────────────────────────────────────────────

def _pnl_color(v: float) -> str:
    return "#2ecc71" if v >= 0 else "#e74c3c"

def _regime_badge(regime: str) -> str:
    colors = {"bull": "#2ecc71", "bear": "#e74c3c", "sideways": "#f39c12"}
    c = colors.get(regime.lower(), "#8b949e")
    return f'<span style="background:{c};color:#000;padding:2px 10px;border-radius:12px;font-weight:700;font-size:.8rem">{regime.upper()}</span>'

def _conv_badge(conv: str) -> str:
    colors = {"ALTA": "#2ecc71", "MEDIA": "#f39c12", "BAJA": "#e74c3c"}
    c = colors.get(conv, "#8b949e")
    return f'<span style="color:{c};font-weight:700">{conv}</span>'

def _fmt_usd(v: float) -> str:
    return f"${v:,.2f}"

def _fmt_pct(v: float) -> str:
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.2f}%"

def _pnl_bar(pnl_pct: float) -> str:
    w = min(abs(pnl_pct) * 4, 100)
    c = _pnl_color(pnl_pct)
    return f'<div style="width:{w:.0f}%;height:6px;background:{c};border-radius:3px;margin-top:3px"></div>'


# ── secciones ─────────────────────────────────────────────────────────────────

def _header(equity: float, initial: float, regime: str, vix: float, wti: float, gold: float) -> str:
    pnl = equity - initial
    pnl_pct = (pnl / initial * 100) if initial else 0
    c = _pnl_color(pnl)
    return f"""
<div class="header-grid">
  <div class="stat-card">
    <div class="stat-label">Equity</div>
    <div class="stat-val" style="color:{c}">{_fmt_usd(equity)}</div>
    <div class="stat-sub">P&L: {_fmt_usd(pnl)} ({_fmt_pct(pnl_pct)})</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Régimen</div>
    <div class="stat-val">{_regime_badge(regime)}</div>
    <div class="stat-sub">Capital base: {_fmt_usd(initial)}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">VIX</div>
    <div class="stat-val" style="color:{'#e74c3c' if vix > 25 else '#f39c12' if vix > 18 else '#2ecc71'}">{vix:.1f}</div>
    <div class="stat-sub">{'Alto — aumentar cautela' if vix > 25 else 'Moderado' if vix > 18 else 'Bajo — mercado tranquilo'}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">WTI / Oro</div>
    <div class="stat-val">${wti:.1f} / ${gold:.0f}</div>
    <div class="stat-sub">Commodities clave</div>
  </div>
</div>"""


def _equity_chart(history: list[dict]) -> str:
    if len(history) < 2:
        return '<div class="card"><p class="sub">Sin historial de equity disponible.</p></div>'
    labels = [datetime.fromtimestamp(h["ts"]).strftime("%d/%m") for h in history]
    vals   = [round(h["equity"], 2) for h in history]
    color  = "#2ecc71" if vals[-1] >= vals[0] else "#e74c3c"
    return f"""
<div class="card" style="flex:2 1 600px">
  <div class="card-title">Curva de Equity</div>
  <canvas id="equityChart" height="120"></canvas>
</div>
<script>
new Chart(document.getElementById('equityChart'), {{
  type: 'line',
  data: {{
    labels: {json.dumps(labels)},
    datasets: [{{
      data: {json.dumps(vals)},
      borderColor: '{color}',
      backgroundColor: '{color}22',
      fill: true,
      tension: 0.4,
      pointRadius: 2,
      borderWidth: 2,
    }}]
  }},
  options: {{
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      x: {{ ticks: {{ color: '#8b949e', maxTicksLimit: 8 }} , grid: {{ color: '#21262d' }} }},
      y: {{ ticks: {{ color: '#8b949e', callback: v => '$' + v.toLocaleString() }}, grid: {{ color: '#21262d' }} }}
    }}
  }}
}});
</script>"""


def _positions_section(positions: list) -> str:
    if not positions:
        return '<div class="card"><div class="card-title">Posiciones</div><p class="sub">Sin posiciones abiertas.</p></div>'

    rows = ""
    for p in positions:
        pnl     = p.unrealized_pl
        pnl_pct = (pnl / (p.avg_price * p.qty) * 100) if p.avg_price and p.qty else 0
        c       = _pnl_color(pnl)
        rows += f"""
  <tr>
    <td><b>{p.ticker}</b></td>
    <td>{p.qty:.2f}</td>
    <td>{_fmt_usd(p.avg_price)}</td>
    <td>{_fmt_usd(p.market_value)}</td>
    <td style="color:{c}">{_fmt_usd(pnl)}<br>{_pnl_bar(pnl_pct)}</td>
    <td style="color:{c};font-weight:700">{_fmt_pct(pnl_pct)}</td>
  </tr>"""

    # Pie chart de allocación
    tickers = [p.ticker for p in positions]
    values  = [round(p.market_value, 2) for p in positions]
    colors  = ["#58a6ff","#2ecc71","#f39c12","#e74c3c","#9b59b6","#1abc9c","#e67e22","#3498db"]

    return f"""
<div class="card">
  <div class="card-title">Posiciones abiertas ({len(positions)})</div>
  <div style="display:flex;gap:20px;flex-wrap:wrap;align-items:flex-start">
    <div style="flex:1;min-width:200px">
      <canvas id="allocChart" width="200" height="200"></canvas>
    </div>
    <div style="flex:2;min-width:300px;overflow-x:auto">
      <table>
        <thead><tr><th>Ticker</th><th>Qty</th><th>Entrada</th><th>Valor</th><th>P&L</th><th>%</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
  </div>
</div>
<script>
new Chart(document.getElementById('allocChart'), {{
  type: 'doughnut',
  data: {{
    labels: {json.dumps(tickers)},
    datasets: [{{ data: {json.dumps(values)},
      backgroundColor: {json.dumps(colors[:len(tickers)])},
      borderWidth: 0 }}]
  }},
  options: {{
    plugins: {{ legend: {{ position: 'bottom', labels: {{ color: '#c9d1d9', font: {{ size: 11 }} }} }} }},
    cutout: '65%'
  }}
}});
</script>"""


def _sector_rotation_section(signals_data: dict) -> str:
    try:
        from alpha_agent.macro.sector_rotation import get_top_sectors
        tops = get_top_sectors(n=8)
    except Exception:
        return ""

    labels = [s for s, _ in tops]
    values = [round(v * 100, 1) for _, v in tops]
    bcolors = ["#2ecc71" if v > 0 else "#e74c3c" for v in values]

    return f"""
<div class="card">
  <div class="card-title">Rotacion Sectorial — Momentum 1-3 meses</div>
  <canvas id="sectorChart" height="100"></canvas>
</div>
<script>
new Chart(document.getElementById('sectorChart'), {{
  type: 'bar',
  data: {{
    labels: {json.dumps(labels)},
    datasets: [{{
      data: {json.dumps(values)},
      backgroundColor: {json.dumps(bcolors)},
      borderRadius: 4
    }}]
  }},
  options: {{
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      x: {{ ticks: {{ color: '#8b949e' }}, grid: {{ display: false }} }},
      y: {{ ticks: {{ color: '#8b949e', callback: v => v + '%' }}, grid: {{ color: '#21262d' }} }}
    }}
  }}
}});
</script>"""


def _signals_section(signals_data: dict) -> str:
    if not signals_data:
        return ""
    gen_at = signals_data.get("generated_at", "")[:16]
    rows = ""
    for bucket, label in [("long_term", "LP"), ("short_term", "CP"), ("options_book", "OPT")]:
        for s in signals_data.get(bucket, []):
            thesis  = s.get("thesis", {})
            risk    = thesis.get("risk", {})
            q       = thesis.get("quant", {})
            conv    = thesis.get("conviction", "?")
            text    = thesis.get("thesis_text", "")[:100]
            sl      = s.get("stop_loss")
            tp      = s.get("take_profit")
            sl_txt  = _fmt_usd(sl) if sl else "—"
            tp_txt  = _fmt_usd(tp) if tp else "—"
            rows += f"""
  <tr>
    <td><b>{s.get('ticker','?')}</b></td>
    <td><span class="badge badge-{label.lower()}">{label}</span></td>
    <td>{_conv_badge(conv)}</td>
    <td>{_fmt_usd(s.get('price', 0))}</td>
    <td>{_fmt_usd(risk.get('dollars_allocated', 0))}</td>
    <td>{q.get('sharpe', 0):.2f}</td>
    <td style="color:#58a6ff">{_fmt_pct(q.get('alpha_jensen', 0) * 100)}</td>
    <td style="color:#e74c3c">{sl_txt}</td>
    <td style="color:#2ecc71">{tp_txt}</td>
    <td class="sub" style="font-size:.75rem;max-width:180px">{text}</td>
  </tr>"""

    return f"""
<div class="card" style="flex:2 1 700px">
  <div class="card-title">Senales del Analyst — {gen_at}</div>
  <div style="overflow-x:auto">
  <table>
    <thead><tr>
      <th>Ticker</th><th>Sleeve</th><th>Conviccion</th><th>Precio</th>
      <th>Asignado</th><th>Sharpe</th><th>Alpha</th><th>SL</th><th>TP</th><th>Tesis</th>
    </tr></thead>
    <tbody>{rows}</tbody>
  </table>
  </div>
</div>"""


def _radar_section(signals_data: dict) -> str:
    radar = signals_data.get("radar", {})
    entries = radar.get("entries", [])
    if not entries:
        return ""
    rows = ""
    for e in entries[:12]:
        t       = e.get("ticker", "")
        move    = e.get("move_pct", 0) or 0
        news    = e.get("top_news", "")[:80]
        c       = _pnl_color(move)
        arrow   = "▲" if move >= 0 else "▼"
        rows += f'<tr><td><b>{t}</b></td><td style="color:{c}">{arrow} {abs(move):.1f}%</td><td class="sub">{news}</td></tr>'

    return f"""
<div class="card">
  <div class="card-title">Radar del Universo</div>
  <table>
    <thead><tr><th>Ticker</th><th>Movimiento</th><th>Noticia</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>"""


# ── CSS y HTML completo ───────────────────────────────────────────────────────

_CSS = """
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Segoe UI', system-ui, sans-serif; background: #0d1117;
       color: #c9d1d9; padding: 16px; max-width: 1400px; margin: 0 auto; }
h1 { color: #58a6ff; font-size: 1.4rem; margin-bottom: 4px; }
.ts { color: #484f58; font-size: .78rem; margin-bottom: 16px; }
.header-grid { display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 16px; }
.stat-card { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
             padding: 14px 18px; flex: 1 1 160px; }
.stat-label { font-size: .75rem; color: #8b949e; text-transform: uppercase;
              letter-spacing: .8px; margin-bottom: 4px; }
.stat-val { font-size: 1.6rem; font-weight: 700; margin-bottom: 2px; }
.stat-sub { font-size: .75rem; color: #8b949e; }
.grid { display: flex; flex-wrap: wrap; gap: 14px; }
.card { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
        padding: 16px; flex: 1 1 380px; }
.card-title { font-size: .8rem; color: #8b949e; text-transform: uppercase;
              letter-spacing: .8px; margin-bottom: 12px; font-weight: 600; }
table { width: 100%; border-collapse: collapse; font-size: .82rem; }
th { color: #8b949e; text-align: left; padding: 5px 8px;
     border-bottom: 1px solid #30363d; font-weight: 500; }
td { padding: 6px 8px; border-bottom: 1px solid #21262d; vertical-align: middle; }
tr:hover td { background: #1c2128; }
.sub { font-size: .78rem; color: #8b949e; }
.badge { padding: 1px 8px; border-radius: 10px; font-size: .72rem; font-weight: 700; }
.badge-lp { background: #1f3a6e; color: #58a6ff; }
.badge-cp { background: #3a2d1f; color: #f39c12; }
.badge-opt { background: #2d1f3a; color: #9b59b6; }
@media (max-width: 600px) { .stat-val { font-size: 1.2rem; } }
"""


def build_html(equity, initial, history, positions, signals_data) -> str:
    macro  = signals_data.get("macro", {})
    regime = macro.get("regime", "unknown")
    prices = macro.get("prices", {})
    vix    = prices.get("vix", 0) or 0
    wti    = prices.get("oil_wti", 0) or 0
    gold   = prices.get("gold", 0) or 0
    now    = datetime.now().strftime("%Y-%m-%d %H:%M")

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="300">
  <title>Alpha Dashboard</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>{_CSS}</style>
</head>
<body>
  <h1>Alpha Trading Dashboard</h1>
  <p class="ts">Actualizado: {now} · Auto-refresh 5 min · Paper Trading</p>
  {_header(equity, initial, regime, vix, wti, gold)}
  <div class="grid">
    {_equity_chart(history)}
    {_sector_rotation_section(signals_data)}
    {_positions_section(positions)}
    {_signals_section(signals_data)}
    {_radar_section(signals_data)}
  </div>
</body>
</html>"""


# ── main ──────────────────────────────────────────────────────────────────────

def generate() -> None:
    from trader_agent.brokers.alpaca_broker import AlpacaBroker
    from alpha_agent.config import PARAMS

    DOCS_DIR.mkdir(exist_ok=True)

    broker = AlpacaBroker(paper=True)
    try:
        equity    = broker.get_equity()
        positions = broker.get_positions()
    except Exception as e:
        logger.error("Error Alpaca: %s", e)
        equity, positions = PARAMS.paper_capital_usd, []

    history: list[dict] = []
    try:
        ph = broker._trading.get_portfolio_history(period="1M", timeframe="1D")
        if ph and ph.equity:
            history = [
                {"ts": t, "equity": float(e)}
                for t, e in zip(ph.timestamp or [], ph.equity)
                if e is not None
            ]
    except Exception as e:
        logger.debug("Portfolio history no disponible: %s", e)

    signals_data: dict = {}
    if SIGNALS_PATH.exists():
        try:
            signals_data = json.loads(SIGNALS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass

    initial = signals_data.get("capital_usd", PARAMS.paper_capital_usd)
    html = build_html(equity, initial, history, positions, signals_data)
    OUT_PATH.write_text(html, encoding="utf-8")
    logger.info("Dashboard generado -> %s", OUT_PATH)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--watch",   action="store_true")
    args = parser.parse_args()

    load_dotenv(BASE_DIR / ".env")
    generate()

    if not args.no_open:
        import webbrowser
        webbrowser.open(OUT_PATH.as_uri())

    if args.watch:
        while True:
            time.sleep(300)
            generate()


if __name__ == "__main__":
    main()
