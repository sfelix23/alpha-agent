"""
Dashboard HTML interactivo — genera docs/index.html (GitHub Pages).

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


# ─── helpers ──────────────────────────────────────────────────────────────────

def _pnl_color(v: float) -> str:
    return "#4ade80" if v >= 0 else "#f87171"

def _pnl_bg(v: float) -> str:
    return "rgba(74,222,128,0.10)" if v >= 0 else "rgba(248,113,113,0.10)"

def _fmt_usd(v: float) -> str:
    return f"${v:,.2f}"

def _fmt_pct(v: float, decimals: int = 2) -> str:
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.{decimals}f}%"

def _regime_info(regime: str) -> tuple[str, str, str]:
    """(label_es, color, icon)"""
    r = regime.lower()
    if r == "bull":
        return "Alcista", "#4ade80", "↑"
    if r == "bear":
        return "Bajista", "#f87171", "↓"
    return "Lateral", "#fbbf24", "→"

def _conv_info(conv: str) -> tuple[str, str]:
    """(label_es, color)"""
    mapping = {
        "ALTA":  ("Alta",  "#4ade80"),
        "MEDIA": ("Media", "#fbbf24"),
        "BAJA":  ("Baja",  "#f87171"),
    }
    return mapping.get(conv, (conv, "#94a3b8"))

def _sleeve_info(bucket: str) -> tuple[str, str]:
    m = {
        "long_term":    ("Largo Plazo",  "#60a5fa"),
        "short_term":   ("Corto Plazo",  "#fbbf24"),
        "options_book": ("Opciones",     "#c084fc"),
        "hedge_book":   ("Cobertura",    "#34d399"),
    }
    return m.get(bucket, (bucket, "#94a3b8"))

def _vix_label(vix: float) -> tuple[str, str]:
    if vix > 30:
        return "Muy alto — mercado en pánico", "#f87171"
    if vix > 25:
        return "Elevado — cautela recomendada", "#fb923c"
    if vix > 18:
        return "Moderado", "#fbbf24"
    return "Bajo — mercado tranquilo", "#4ade80"

def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# ─── secciones HTML ───────────────────────────────────────────────────────────

def _header_section(equity: float, initial: float, regime: str,
                    vix: float, wti: float, gold: float, dxy: float) -> str:
    pnl     = equity - initial
    pnl_pct = (pnl / initial * 100) if initial else 0
    pnl_c   = _pnl_color(pnl)
    pnl_bg  = _pnl_bg(pnl)
    reg_label, reg_color, reg_icon = _regime_info(regime)
    vix_label, vix_color = _vix_label(vix)

    return f"""
<div class="kpi-grid">
  <div class="kpi-card kpi-main">
    <div class="kpi-icon">&#x1F4BC;</div>
    <div class="kpi-label">Patrimonio Total</div>
    <div class="kpi-value" style="color:{pnl_c}">{_fmt_usd(equity)}</div>
    <div class="kpi-sub" style="background:{pnl_bg};color:{pnl_c};border-radius:6px;padding:3px 10px;display:inline-block;margin-top:6px">
      {_fmt_usd(pnl)} &nbsp;({_fmt_pct(pnl_pct)}) desde inicio
    </div>
  </div>
  <div class="kpi-card">
    <div class="kpi-icon">&#x1F4CA;</div>
    <div class="kpi-label">Régimen de Mercado</div>
    <div class="kpi-value" style="color:{reg_color}">{reg_icon} {reg_label}</div>
    <div class="kpi-sub">Capital base: {_fmt_usd(initial)}</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-icon">&#x26A1;</div>
    <div class="kpi-label">VIX — Volatilidad</div>
    <div class="kpi-value" style="color:{vix_color}">{vix:.1f}</div>
    <div class="kpi-sub">{vix_label}</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-icon">&#x1F6E2;</div>
    <div class="kpi-label">Petróleo WTI</div>
    <div class="kpi-value">{_fmt_usd(wti)}</div>
    <div class="kpi-sub">por barril</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-icon">&#x1F947;</div>
    <div class="kpi-label">Oro</div>
    <div class="kpi-value">{_fmt_usd(gold)}</div>
    <div class="kpi-sub">por onza troy</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-icon">&#x1F4B5;</div>
    <div class="kpi-label">Dólar Index (DXY)</div>
    <div class="kpi-value">{dxy:.1f}</div>
    <div class="kpi-sub">fuerza del dólar</div>
  </div>
</div>"""


def _equity_chart_section(history: list[dict]) -> str:
    if len(history) < 2:
        return '<div class="card"><p class="muted">Sin historial de patrimonio disponible todavía.</p></div>'

    labels = [datetime.fromtimestamp(h["ts"]).strftime("%d %b") for h in history]
    vals   = [round(h["equity"], 2) for h in history]
    first  = vals[0]
    last   = vals[-1]
    color  = "#4ade80" if last >= first else "#f87171"
    min_v  = min(vals) * 0.995
    max_v  = max(vals) * 1.005

    return f"""
<div class="card card-wide">
  <div class="card-header">
    <span class="card-title">Evolución del Patrimonio</span>
    <span class="card-badge" style="color:{color}">{_fmt_pct((last - first)/first*100 if first else 0)} total</span>
  </div>
  <canvas id="equityChart" height="90"></canvas>
</div>
<script>
(function(){{
  const ctx = document.getElementById('equityChart');
  const grad = ctx.getContext('2d').createLinearGradient(0, 0, 0, 280);
  grad.addColorStop(0, '{color}44');
  grad.addColorStop(1, '{color}00');
  new Chart(ctx, {{
    type: 'line',
    data: {{
      labels: {json.dumps(labels)},
      datasets: [{{
        data: {json.dumps(vals)},
        borderColor: '{color}',
        backgroundColor: grad,
        fill: true,
        tension: 0.4,
        pointRadius: 3,
        pointHoverRadius: 6,
        pointBackgroundColor: '{color}',
        borderWidth: 2.5,
      }}]
    }},
    options: {{
      animation: {{ duration: 800, easing: 'easeInOutQuart' }},
      plugins: {{
        legend: {{ display: false }},
        tooltip: {{
          backgroundColor: '#1e2837',
          titleColor: '#94a3b8',
          bodyColor: '#f1f5f9',
          padding: 12,
          callbacks: {{
            label: ctx => ' $' + ctx.parsed.y.toLocaleString('es-AR', {{minimumFractionDigits:2}})
          }}
        }}
      }},
      scales: {{
        x: {{
          ticks: {{ color: '#64748b', maxTicksLimit: 10, font: {{ size: 11 }} }},
          grid: {{ color: '#1e293b' }}
        }},
        y: {{
          min: {min_v:.2f},
          max: {max_v:.2f},
          ticks: {{ color: '#64748b', callback: v => '$' + v.toLocaleString(), font: {{ size: 11 }} }},
          grid: {{ color: '#1e293b' }}
        }}
      }}
    }}
  }});
}})();
</script>"""


def _positions_section(positions: list) -> str:
    if not positions:
        return '<div class="card"><div class="card-title">Posiciones Abiertas</div><p class="muted">Sin posiciones abiertas en este momento.</p></div>'

    tickers  = []
    values   = []
    pnl_rows = []
    chart_colors = [
        "#60a5fa","#4ade80","#fbbf24","#f87171","#c084fc",
        "#34d399","#fb923c","#38bdf8","#a78bfa","#f472b6",
    ]

    rows_html = ""
    for i, p in enumerate(positions):
        cost        = (p.avg_price or 0) * (p.qty or 0)
        pnl         = p.unrealized_pl
        pnl_pct     = (pnl / cost * 100) if cost else 0
        pnl_c       = _pnl_color(pnl)
        bar_w       = min(abs(pnl_pct) * 5, 100)
        color       = chart_colors[i % len(chart_colors)]
        tickers.append(p.ticker)
        values.append(round(p.market_value, 2))

        rows_html += f"""
<div class="pos-card" style="border-left:3px solid {color}">
  <div class="pos-top">
    <span class="pos-ticker" style="color:{color}">{p.ticker}</span>
    <span class="pos-pnl" style="color:{pnl_c};background:rgba(0,0,0,.2);padding:2px 10px;border-radius:20px">
      {_fmt_pct(pnl_pct, 1)} &nbsp; {_fmt_usd(pnl)}
    </span>
  </div>
  <div class="pos-bar-track">
    <div class="pos-bar-fill" style="width:{bar_w:.1f}%;background:{pnl_c}"></div>
  </div>
  <div class="pos-details">
    <span>Cantidad: <b>{p.qty:.4f}</b></span>
    <span>Precio entrada: <b>{_fmt_usd(p.avg_price)}</b></span>
    <span>Valor de mercado: <b>{_fmt_usd(p.market_value)}</b></span>
  </div>
</div>"""

    return f"""
<div class="card">
  <div class="card-header">
    <span class="card-title">Posiciones Abiertas</span>
    <span class="card-badge">{len(positions)} activos</span>
  </div>
  <div style="display:flex;gap:24px;flex-wrap:wrap;align-items:flex-start">
    <div style="flex:0 0 200px;display:flex;flex-direction:column;align-items:center">
      <canvas id="allocChart" width="180" height="180"></canvas>
      <div class="chart-legend" id="allocLegend"></div>
    </div>
    <div style="flex:1;min-width:300px">
      {rows_html}
    </div>
  </div>
</div>
<script>
(function(){{
  const labels = {json.dumps(tickers)};
  const values = {json.dumps(values)};
  const colors = {json.dumps(chart_colors[:len(tickers)])};
  new Chart(document.getElementById('allocChart'), {{
    type: 'doughnut',
    data: {{ labels, datasets: [{{ data: values, backgroundColor: colors, borderWidth: 2, borderColor: '#0f172a' }}] }},
    options: {{
      cutout: '68%',
      animation: {{ duration: 900, easing: 'easeInOutQuart' }},
      plugins: {{
        legend: {{ display: false }},
        tooltip: {{
          backgroundColor: '#1e2837',
          titleColor: '#94a3b8',
          bodyColor: '#f1f5f9',
          padding: 10,
          callbacks: {{
            label: ctx => ' $' + ctx.parsed.toLocaleString('es-AR', {{minimumFractionDigits:2}})
          }}
        }}
      }}
    }}
  }});
  const leg = document.getElementById('allocLegend');
  const total = values.reduce((a,b)=>a+b,0);
  labels.forEach((l,i)=>{{
    const pct = (values[i]/total*100).toFixed(1);
    leg.innerHTML += `<div class="leg-item"><span class="leg-dot" style="background:${{colors[i]}}"></span>${{l}} <span class="muted">${{pct}}%</span></div>`;
  }});
}})();
</script>"""


def _sector_section() -> str:
    try:
        from alpha_agent.macro.sector_rotation import get_top_sectors
        tops = get_top_sectors(n=10)
    except Exception:
        return ""

    if not tops:
        return ""

    labels  = [s for s, _ in tops]
    values  = [round(v * 100, 2) for _, v in tops]
    colors  = ["#4ade80" if v >= 0 else "#f87171" for v in values]

    return f"""
<div class="card">
  <div class="card-header">
    <span class="card-title">Rotación Sectorial</span>
    <span class="card-badge muted">Impulso 1-3 meses</span>
  </div>
  <canvas id="sectorChart" height="110"></canvas>
</div>
<script>
(function(){{
  new Chart(document.getElementById('sectorChart'), {{
    type: 'bar',
    data: {{
      labels: {json.dumps(labels)},
      datasets: [{{
        data: {json.dumps(values)},
        backgroundColor: {json.dumps(colors)},
        borderRadius: 6,
        borderSkipped: false,
      }}]
    }},
    options: {{
      animation: {{ duration: 700 }},
      plugins: {{
        legend: {{ display: false }},
        tooltip: {{
          backgroundColor: '#1e2837',
          titleColor: '#94a3b8',
          bodyColor: '#f1f5f9',
          padding: 10,
          callbacks: {{ label: ctx => ' ' + ctx.parsed.y.toFixed(2) + '%' }}
        }}
      }},
      scales: {{
        x: {{ ticks: {{ color: '#64748b', font: {{ size: 11 }} }}, grid: {{ display: false }} }},
        y: {{
          ticks: {{ color: '#64748b', callback: v => v + '%', font: {{ size: 11 }} }},
          grid: {{ color: '#1e293b' }}
        }}
      }}
    }}
  }});
}})();
</script>"""


def _signals_section(signals_data: dict) -> str:
    gen_at = signals_data.get("generated_at", "")[:16].replace("T", " ")
    cards  = ""

    for bucket in ("long_term", "short_term", "options_book", "hedge_book"):
        sleeve_label, sleeve_color = _sleeve_info(bucket)
        for s in signals_data.get(bucket, []):
            thesis    = s.get("thesis", {})
            quant     = thesis.get("quant", {})
            risk      = thesis.get("risk", {})
            conv      = thesis.get("conviction", "?")
            conv_label, conv_color = _conv_info(conv)
            thesis_text = _escape(thesis.get("thesis_text", "Sin descripción disponible."))
            sl        = s.get("stop_loss")
            tp        = s.get("take_profit")
            price     = s.get("price", 0) or 0
            sharpe    = quant.get("sharpe", 0) or 0
            alpha     = (quant.get("alpha_jensen", 0) or 0) * 100
            beta      = quant.get("beta", 0) or 0
            alloc     = risk.get("dollars_allocated", 0) or 0
            ticker    = s.get("ticker", "?")

            # Barra de convicción visual
            conv_pct = {"ALTA": 90, "MEDIA": 55, "BAJA": 25}.get(conv, 40)

            sl_html = f'<span style="color:#f87171">{_fmt_usd(sl)}</span>' if sl else '<span class="muted">—</span>'
            tp_html = f'<span style="color:#4ade80">{_fmt_usd(tp)}</span>' if tp else '<span class="muted">—</span>'

            uid = f"sig_{ticker}_{bucket}"
            cards += f"""
<div class="sig-card" onclick="toggleThesis('{uid}')">
  <div class="sig-top">
    <div style="display:flex;align-items:center;gap:10px">
      <span class="sig-ticker">{ticker}</span>
      <span class="sig-sleeve" style="color:{sleeve_color};border-color:{sleeve_color}40">{sleeve_label}</span>
      <span class="sig-conv" style="color:{conv_color}">&#9679; Convicción {conv_label}</span>
    </div>
    <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap">
      <span class="sig-price">Precio: <b>{_fmt_usd(price)}</b></span>
      <span class="sig-price">Asignado: <b>{_fmt_usd(alloc)}</b></span>
      <span class="sig-expand">Ver análisis &#9660;</span>
    </div>
  </div>
  <div class="sig-metrics">
    <div class="metric-pill">
      <div class="metric-label">Sharpe</div>
      <div class="metric-val" style="color:#60a5fa">{sharpe:.2f}</div>
    </div>
    <div class="metric-pill">
      <div class="metric-label">Alfa de Jensen</div>
      <div class="metric-val" style="color:{'#4ade80' if alpha >= 0 else '#f87171'}">{_fmt_pct(alpha)}</div>
    </div>
    <div class="metric-pill">
      <div class="metric-label">Beta</div>
      <div class="metric-val">{beta:.2f}</div>
    </div>
    <div class="metric-pill">
      <div class="metric-label">Stop Loss</div>
      <div class="metric-val">{sl_html}</div>
    </div>
    <div class="metric-pill">
      <div class="metric-label">Toma de Ganancias</div>
      <div class="metric-val">{tp_html}</div>
    </div>
  </div>
  <div class="conv-bar-track">
    <div class="conv-bar-fill" style="width:{conv_pct}%;background:{conv_color}"></div>
  </div>
  <div class="sig-thesis" id="{uid}" style="display:none">
    <p>{thesis_text}</p>
  </div>
</div>"""

    if not cards:
        return '<div class="card"><p class="muted">Sin señales disponibles. Ejecute el Agente Analista primero.</p></div>'

    return f"""
<div class="card card-wide">
  <div class="card-header">
    <span class="card-title">Señales del Agente Analista</span>
    <span class="card-badge muted">Generadas: {gen_at}</span>
  </div>
  <p class="muted" style="margin-bottom:12px;font-size:.8rem">
    Haga clic en cada señal para ver el análisis completo.
  </p>
  {cards}
</div>"""


def _radar_section(signals_data: dict) -> str:
    radar   = signals_data.get("radar", {})
    entries = radar.get("entries", [])
    n_up    = radar.get("n_up", 0)
    n_down  = radar.get("n_down", 0)
    winner  = radar.get("biggest_winner", "—")
    loser   = radar.get("biggest_loser", "—")

    if not entries:
        return ""

    rows = ""
    for e in entries[:14]:
        t      = e.get("ticker", "")
        move   = e.get("move_pct", 0) or 0
        news   = _escape(e.get("top_news", "Sin noticias")[:100])
        action = _escape(e.get("bot_action", ""))
        c      = _pnl_color(move)
        arrow  = "▲" if move >= 0 else "▼"
        rows  += f"""
<tr>
  <td><b style="color:#e2e8f0">{t}</b></td>
  <td style="color:{c};font-weight:700;white-space:nowrap">{arrow} {abs(move):.1f}%</td>
  <td class="muted" style="font-size:.8rem">{news}</td>
  <td><span style="font-size:.75rem;color:#94a3b8">{action}</span></td>
</tr>"""

    return f"""
<div class="card card-wide">
  <div class="card-header">
    <span class="card-title">Radar del Universo — 51 Activos</span>
    <div style="display:flex;gap:14px;flex-wrap:wrap">
      <span style="color:#4ade80">&#9650; {n_up} al alza</span>
      <span style="color:#f87171">&#9660; {n_down} a la baja</span>
      <span class="muted">Mayor ganador: <b style="color:#4ade80">{winner}</b></span>
      <span class="muted">Mayor perdedor: <b style="color:#f87171">{loser}</b></span>
    </div>
  </div>
  <div style="overflow-x:auto">
    <table>
      <thead>
        <tr>
          <th>Activo</th>
          <th>Movimiento</th>
          <th>Noticia destacada</th>
          <th>Acción del sistema</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
</div>"""


def _pnl_calendar_section(history: list[dict]) -> str:
    """
    Calendario mensual de P&L diario.
    Cada celda muestra el día, el P&L en dólares y el % de variación.
    Verde = ganancia, Rojo = pérdida, intensidad proporcional al monto.
    """
    if len(history) < 2:
        return ""

    # Calcular P&L diario desde la curva de equity
    from datetime import date, timedelta
    import calendar as cal_mod

    daily: dict[date, dict] = {}
    for i in range(1, len(history)):
        prev_eq = history[i - 1]["equity"]
        curr_eq = history[i]["equity"]
        d       = datetime.fromtimestamp(history[i]["ts"]).date()
        pnl     = curr_eq - prev_eq
        pnl_pct = (pnl / prev_eq * 100) if prev_eq else 0
        daily[d] = {"pnl": pnl, "pnl_pct": pnl_pct, "equity": curr_eq}

    if not daily:
        return ""

    # Mes y año del último dato disponible
    last_date  = max(daily.keys())
    year, month = last_date.year, last_date.month
    month_name  = ["Enero","Febrero","Marzo","Abril","Mayo","Junio",
                   "Julio","Agosto","Septiembre","Octubre","Noviembre","Diciembre"][month - 1]

    # Estadísticas del mes
    month_days  = {d: v for d, v in daily.items() if d.year == year and d.month == month}
    total_pnl   = sum(v["pnl"] for v in month_days.values())
    wins        = sum(1 for v in month_days.values() if v["pnl"] >= 0)
    losses      = sum(1 for v in month_days.values() if v["pnl"] < 0)
    best        = max(month_days.values(), key=lambda v: v["pnl"], default=None)
    worst       = min(month_days.values(), key=lambda v: v["pnl"], default=None)
    total_c     = "#4ade80" if total_pnl >= 0 else "#f87171"

    def _cell_colors(pnl: float) -> tuple[str, str]:
        """(background, text color)"""
        if pnl > 0:
            intensity = min(pnl / 300, 1.0)   # satura en +$300
            r = int(10  + (22 - 10)  * intensity)
            g = int(40  + (163 - 40) * intensity)
            b = int(30  + (50 - 30)  * intensity)
            return f"rgb({r},{g},{b})", "#4ade80" if intensity > 0.4 else "#86efac"
        elif pnl < 0:
            intensity = min(abs(pnl) / 300, 1.0)
            r = int(50  + (200 - 50) * intensity)
            g = int(10  + (30 - 10)  * intensity)
            b = int(10  + (30 - 10)  * intensity)
            return f"rgb({r},{g},{b})", "#f87171" if intensity > 0.4 else "#fca5a5"
        return "var(--surface2)", "#64748b"

    # Construir grilla — semanas como filas, Lun-Dom como columnas
    first_day  = date(year, month, 1)
    last_day_n = cal_mod.monthrange(year, month)[1]
    # Índice lunes=0 del primer día del mes
    start_dow  = first_day.weekday()

    headers = ["".join(f'<th class="cal-th">{d}</th>'
               for d in ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"])]

    rows_html = ""
    day_num   = 1 - start_dow   # puede ser negativo para celdas vacías iniciales

    while day_num <= last_day_n:
        rows_html += "<tr>"
        for dow in range(7):
            if day_num < 1 or day_num > last_day_n:
                rows_html += '<td class="cal-empty"></td>'
            else:
                d = date(year, month, day_num)
                is_weekend = dow >= 5
                data = month_days.get(d)

                if data:
                    bg, tc = _cell_colors(data["pnl"])
                    sign   = "+" if data["pnl"] >= 0 else ""
                    rows_html += f"""
<td class="cal-cell" style="background:{bg}">
  <div class="cal-day">{day_num}</div>
  <div class="cal-pnl" style="color:{tc}">{sign}${data['pnl']:,.0f}</div>
  <div class="cal-pct" style="color:{tc}">{sign}{data['pnl_pct']:.1f}%</div>
</td>"""
                elif is_weekend:
                    rows_html += f'<td class="cal-cell cal-weekend"><div class="cal-day">{day_num}</div></td>'
                else:
                    rows_html += f'<td class="cal-cell cal-no-data"><div class="cal-day">{day_num}</div><div class="cal-pct">—</div></td>'
            day_num += 1
        rows_html += "</tr>"

    best_html  = f'<b style="color:#4ade80">+${best["pnl"]:,.0f}</b>' if best else "—"
    worst_html = f'<b style="color:#f87171">${worst["pnl"]:,.0f}</b>' if worst else "—"

    return f"""
<div class="card card-wide">
  <div class="card-header">
    <span class="card-title">Calendario de Resultados — {month_name} {year}</span>
    <div style="display:flex;gap:16px;flex-wrap:wrap;align-items:center">
      <span style="color:{total_c};font-weight:700;font-size:.95rem">
        {"+" if total_pnl >= 0 else ""}${total_pnl:,.0f} en el mes
      </span>
      <span class="muted">{wins} días positivos · {losses} negativos</span>
      <span class="muted">Mejor: {best_html} · Peor: {worst_html}</span>
    </div>
  </div>
  <div style="overflow-x:auto">
    <table class="cal-table">
      <thead><tr>{"".join(f'<th class="cal-th">{d}</th>' for d in ["Lun","Mar","Mié","Jue","Vie","Sáb","Dom"])}</tr></thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>
</div>"""


def _risk_metrics_section(signals_data: dict, equity: float, initial: float) -> str:
    port    = signals_data.get("portfolio", {})
    sharpe  = port.get("sharpe", 0) or 0
    ret_exp = (port.get("exp_return", 0) or 0) * 100
    vol     = (port.get("volatility", 0) or 0) * 100
    macro   = signals_data.get("macro", {})
    vix     = (macro.get("prices", {}) or {}).get("vix", 0) or 0
    dd_limit = 10.0
    pnl_pct = ((equity - initial) / initial * 100) if initial else 0

    # VIX sizing multiplier
    if vix > 30:
        sizing = 60
        sizing_label = "60% — alta volatilidad"
    elif vix > 25:
        sizing = 75
        sizing_label = "75% — volatilidad elevada"
    elif vix < 15:
        sizing = 110
        sizing_label = "110% — volatilidad baja"
    else:
        sizing = 100
        sizing_label = "100% — normal"

    return f"""
<div class="kpi-grid kpi-grid-sm">
  <div class="kpi-card">
    <div class="kpi-label">Ratio Sharpe</div>
    <div class="kpi-value" style="color:{'#4ade80' if sharpe > 0.5 else '#fbbf24' if sharpe > 0 else '#f87171'}">{sharpe:.2f}</div>
    <div class="kpi-sub">Retorno ajustado por riesgo</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">Retorno Esperado (anual)</div>
    <div class="kpi-value" style="color:#60a5fa">{_fmt_pct(ret_exp)}</div>
    <div class="kpi-sub">Volatilidad anualizada: {_fmt_pct(vol)}</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">Rentabilidad desde inicio</div>
    <div class="kpi-value" style="color:{'#4ade80' if pnl_pct >= 0 else '#f87171'}">{_fmt_pct(pnl_pct)}</div>
    <div class="kpi-sub">Capital inicial: {_fmt_usd(initial)}</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">Tamaño de Posición (VIX)</div>
    <div class="kpi-value" style="color:#fbbf24">{sizing}%</div>
    <div class="kpi-sub">{sizing_label}</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">Drawdown Máximo Permitido</div>
    <div class="kpi-value" style="color:#f87171">-{dd_limit:.0f}%</div>
    <div class="kpi-sub">Kill switch intradía: -3%</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">Régimen de riesgo (VIX)</div>
    <div class="kpi-value" style="color:{_vix_label(vix)[1]}">{vix:.1f}</div>
    <div class="kpi-sub">{_vix_label(vix)[0]}</div>
  </div>
</div>"""


# ─── CSS ──────────────────────────────────────────────────────────────────────

_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg:       #0a0f1a;
  --surface:  #111827;
  --surface2: #1e2837;
  --border:   #1e293b;
  --text:     #e2e8f0;
  --muted:    #64748b;
  --accent:   #3b82f6;
}

body {
  font-family: 'Inter', system-ui, sans-serif;
  background: var(--bg);
  color: var(--text);
  padding: 20px 24px 48px;
  max-width: 1440px;
  margin: 0 auto;
  line-height: 1.5;
}

/* ── header de página ── */
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 28px;
  padding-bottom: 18px;
  border-bottom: 1px solid var(--border);
}
.page-title {
  font-size: 1.5rem;
  font-weight: 800;
  background: linear-gradient(135deg, #60a5fa, #a78bfa);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}
.page-meta { font-size: .8rem; color: var(--muted); }
.live-dot {
  display: inline-block;
  width: 8px; height: 8px;
  background: #4ade80;
  border-radius: 50%;
  margin-right: 6px;
  animation: pulse 2s infinite;
}
@keyframes pulse {
  0%,100% { opacity: 1; }
  50% { opacity: .4; }
}

/* ── KPI grid ── */
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 14px;
  margin-bottom: 22px;
}
.kpi-grid-sm {
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
}
.kpi-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 18px 20px;
  transition: transform .15s, box-shadow .15s;
}
.kpi-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 8px 24px rgba(0,0,0,.4);
}
.kpi-main {
  background: linear-gradient(135deg, #111827, #1e2837);
  border-color: #3b82f622;
}
.kpi-icon { font-size: 1.3rem; margin-bottom: 6px; }
.kpi-label {
  font-size: .72rem;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: .7px;
  margin-bottom: 6px;
  font-weight: 600;
}
.kpi-value {
  font-size: 1.55rem;
  font-weight: 700;
  line-height: 1.1;
  margin-bottom: 4px;
}
.kpi-sub { font-size: .75rem; color: var(--muted); }

/* ── cards generales ── */
.section-title {
  font-size: .75rem;
  font-weight: 700;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: .8px;
  margin: 28px 0 12px;
}
.grid { display: flex; flex-wrap: wrap; gap: 16px; }

.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 20px 22px;
  flex: 1 1 380px;
  transition: box-shadow .15s;
}
.card:hover { box-shadow: 0 6px 20px rgba(0,0,0,.35); }
.card-wide { flex: 2 1 700px; }

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 16px;
}
.card-title {
  font-size: .9rem;
  font-weight: 700;
  color: var(--text);
  letter-spacing: .2px;
}
.card-badge {
  font-size: .75rem;
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 20px;
  padding: 2px 10px;
  font-weight: 600;
}

/* ── posiciones ── */
.pos-card {
  background: var(--surface2);
  border-radius: 8px;
  padding: 12px 14px;
  margin-bottom: 10px;
  cursor: default;
  transition: background .15s;
}
.pos-card:hover { background: #263347; }
.pos-top {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}
.pos-ticker { font-size: 1rem; font-weight: 800; }
.pos-pnl { font-size: .85rem; font-weight: 600; }
.pos-bar-track {
  height: 4px;
  background: #1e293b;
  border-radius: 2px;
  margin-bottom: 8px;
}
.pos-bar-fill {
  height: 4px;
  border-radius: 2px;
  transition: width .6s ease;
}
.pos-details {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
  font-size: .78rem;
  color: var(--muted);
}
.pos-details b { color: var(--text); }

/* ── legend ── */
.chart-legend { margin-top: 12px; width: 100%; }
.leg-item {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: .76rem;
  color: var(--text);
  margin-bottom: 4px;
}
.leg-dot {
  width: 8px; height: 8px;
  border-radius: 2px;
  flex-shrink: 0;
}

/* ── señales ── */
.sig-card {
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px 16px;
  margin-bottom: 10px;
  cursor: pointer;
  transition: background .15s, box-shadow .15s;
  user-select: none;
}
.sig-card:hover { background: #1a2535; box-shadow: 0 4px 12px rgba(0,0,0,.3); }
.sig-top {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 10px;
}
.sig-ticker { font-size: 1.05rem; font-weight: 800; }
.sig-sleeve {
  font-size: .72rem;
  font-weight: 700;
  border: 1px solid;
  border-radius: 20px;
  padding: 2px 9px;
}
.sig-conv { font-size: .8rem; font-weight: 600; }
.sig-price { font-size: .8rem; color: var(--muted); }
.sig-expand { font-size: .75rem; color: var(--muted); letter-spacing: .3px; }

.sig-metrics {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.metric-pill {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 6px 12px;
  text-align: center;
  min-width: 90px;
}
.metric-label { font-size: .68rem; color: var(--muted); margin-bottom: 2px; }
.metric-val { font-size: .88rem; font-weight: 700; }

.conv-bar-track {
  height: 3px;
  background: var(--border);
  border-radius: 2px;
  margin-bottom: 0;
}
.conv-bar-fill {
  height: 3px;
  border-radius: 2px;
  transition: width .5s ease;
  opacity: .7;
}

.sig-thesis {
  margin-top: 12px;
  padding: 12px;
  background: var(--surface);
  border-radius: 8px;
  font-size: .83rem;
  color: #94a3b8;
  line-height: 1.6;
  border-left: 3px solid #3b82f6;
  animation: fadeIn .2s ease;
}
@keyframes fadeIn { from { opacity:0; transform:translateY(-4px); } to { opacity:1; transform:none; } }

/* ── tabla ── */
table { width: 100%; border-collapse: collapse; font-size: .83rem; }
th {
  color: var(--muted);
  text-align: left;
  padding: 7px 10px;
  border-bottom: 1px solid var(--border);
  font-weight: 600;
  font-size: .75rem;
  text-transform: uppercase;
  letter-spacing: .5px;
}
td {
  padding: 9px 10px;
  border-bottom: 1px solid var(--border);
  vertical-align: middle;
}
tr:last-child td { border-bottom: none; }
tr:hover td { background: var(--surface2); }

/* ── calendario P&L ── */
.cal-table {
  border-collapse: separate;
  border-spacing: 4px;
  width: 100%;
  table-layout: fixed;
}
.cal-th {
  color: var(--muted);
  font-size: .72rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: .5px;
  padding: 6px 4px;
  text-align: center;
  border-bottom: none;
}
.cal-cell {
  border-radius: 8px;
  padding: 8px 6px;
  text-align: center;
  vertical-align: top;
  min-width: 70px;
  height: 72px;
  background: var(--surface2);
  transition: transform .1s, box-shadow .1s;
  border-bottom: none;
}
.cal-cell:hover {
  transform: scale(1.04);
  box-shadow: 0 4px 16px rgba(0,0,0,.5);
  z-index: 2;
  position: relative;
}
.cal-empty {
  border-bottom: none;
}
.cal-weekend { opacity: .45; }
.cal-no-data { opacity: .55; }
.cal-day {
  font-size: .7rem;
  color: var(--muted);
  font-weight: 600;
  margin-bottom: 4px;
}
.cal-pnl {
  font-size: .82rem;
  font-weight: 700;
  line-height: 1.2;
}
.cal-pct {
  font-size: .7rem;
  margin-top: 2px;
  opacity: .85;
}
.cal-cell:hover .cal-pnl { font-size: .88rem; }

/* ── misc ── */
.muted { color: var(--muted); }
.refresh-bar {
  height: 2px;
  background: linear-gradient(90deg, #3b82f6, #a78bfa);
  position: fixed;
  top: 0; left: 0;
  width: 0%;
  transition: width 300s linear;
  border-radius: 0 2px 2px 0;
}

@media (max-width: 640px) {
  body { padding: 12px; }
  .kpi-value { font-size: 1.2rem; }
  .card { flex: 1 1 100%; }
}
"""


# ─── HTML completo ─────────────────────────────────────────────────────────────

def build_html(equity: float, initial: float, history: list[dict],
               positions: list, signals_data: dict) -> str:
    macro  = signals_data.get("macro", {})
    regime = macro.get("regime", "unknown")
    prices = macro.get("prices", {}) or {}
    vix    = prices.get("vix", 0) or 0
    wti    = prices.get("oil_wti", 0) or 0
    gold   = prices.get("gold", 0) or 0
    dxy    = prices.get("dxy", 0) or 0
    now    = datetime.now().strftime("%d de %B de %Y — %H:%M")

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="300">
  <title>Alpha Trading Dashboard</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
  <style>{_CSS}</style>
</head>
<body>

<div class="refresh-bar" id="rbar"></div>

<div class="page-header">
  <div>
    <div class="page-title">Alpha Trading Dashboard</div>
    <div class="page-meta"><span class="live-dot"></span>Actualizado el {now} &middot; Se actualiza automáticamente cada 5 minutos &middot; Paper Trading</div>
  </div>
  <div id="countdown" style="font-size:.8rem;color:#64748b"></div>
</div>

<p class="section-title">Resumen del Portafolio</p>
{_header_section(equity, initial, regime, vix, wti, gold, dxy)}

<p class="section-title">Métricas de Riesgo</p>
{_risk_metrics_section(signals_data, equity, initial)}

<p class="section-title">Curva de Patrimonio</p>
<div class="grid">
  {_equity_chart_section(history)}
  {_sector_section()}
</div>

<p class="section-title">Calendario de Resultados</p>
{_pnl_calendar_section(history)}

<p class="section-title">Posiciones Abiertas</p>
<div class="grid">
  {_positions_section(positions)}
</div>

<p class="section-title">Señales y Análisis</p>
{_signals_section(signals_data)}

<p class="section-title">Radar del Universo</p>
{_radar_section(signals_data)}

<script>
function toggleThesis(id) {{
  const el = document.getElementById(id);
  const card = el.closest('.sig-card');
  const exp  = card.querySelector('.sig-expand');
  if (el.style.display === 'none') {{
    el.style.display = 'block';
    exp.textContent = 'Ocultar análisis ▲';
  }} else {{
    el.style.display = 'none';
    exp.textContent = 'Ver análisis ▼';
  }}
}}

// Barra de progreso hacia el próximo refresh (5 min)
setTimeout(() => document.getElementById('rbar').style.width = '100%', 100);

// Countdown
(function() {{
  let secs = 300;
  const el = document.getElementById('countdown');
  setInterval(() => {{
    secs--;
    if (secs <= 0) return;
    const m = Math.floor(secs / 60);
    const s = secs % 60;
    el.textContent = `Próxima actualización en ${{m}}:${{s.toString().padStart(2,'0')}}`;
  }}, 1000);
}})();
</script>

</body>
</html>"""


# ─── main ─────────────────────────────────────────────────────────────────────

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
        from alpaca.trading.requests import GetPortfolioHistoryRequest
        req = GetPortfolioHistoryRequest(period="1M", timeframe="1D")
        ph  = broker._trading.get_portfolio_history(req)
        if ph and ph.equity:
            history = [
                {"ts": t, "equity": float(e)}
                for t, e in zip(ph.timestamp or [], ph.equity)
                if e is not None
            ]
        logger.info("Portfolio history: %d entradas", len(history))
    except Exception as e:
        logger.warning("Portfolio history no disponible: %s", e)

    signals_data: dict = {}
    if SIGNALS_PATH.exists():
        try:
            signals_data = json.loads(SIGNALS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass

    initial = signals_data.get("capital_usd", PARAMS.paper_capital_usd)
    html    = build_html(equity, initial, history, positions, signals_data)
    OUT_PATH.write_text(html, encoding="utf-8")
    logger.info("Dashboard generado -> %s (%d bytes)", OUT_PATH, len(html))


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
