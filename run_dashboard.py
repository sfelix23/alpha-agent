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
from datetime import datetime, timezone
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

def _c(v: float) -> str:
    return "#3fb950" if v >= 0 else "#f85149"

def _bg(v: float) -> str:
    return "rgba(63,185,80,.15)" if v >= 0 else "rgba(248,81,73,.15)"

def _usd(v: float) -> str:
    return f"${v:,.2f}"

def _pct(v: float, d: int = 2) -> str:
    return f"{'+'if v>=0 else ''}{v:.{d}f}%"

def _esc(s: str) -> str:
    return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;")

def _regime(r: str) -> tuple[str, str]:
    m = {"bull": ("Alcista","#3fb950"), "bear": ("Bajista","#f85149")}
    return m.get(r.lower(), ("Lateral","#d29922"))

def _conv(c: str) -> tuple[str, str]:
    m = {"ALTA":("Alta","#3fb950"), "MEDIA":("Media","#d29922"), "BAJA":("Baja","#f85149")}
    return m.get(c, (c,"#7d8590"))

def _sleeve(b: str) -> tuple[str, str]:
    m = {
        "long_term":    ("Largo Plazo", "#58a6ff"),
        "short_term":   ("Corto Plazo", "#d29922"),
        "options_book": ("Opciones",    "#bc8cff"),
        "hedge_book":   ("Cobertura",   "#3fb950"),
    }
    return m.get(b, (b, "#7d8590"))

def _vix_info(v: float) -> tuple[str, str]:
    if v > 30: return "Panico — sizing 60%", "#f85149"
    if v > 25: return "Elevado — sizing 75%", "#ffa657"
    if v > 18: return "Moderado — sizing 100%", "#d29922"
    return "Tranquilo — sizing 110%", "#3fb950"

def _calc_metrics(history: list[dict], spy_history: list[dict], qqq_history: list[dict] | None = None) -> dict:
    if len(history) < 3:
        return {}
    vals = [h["equity"] for h in history]
    daily_rets = [(vals[i] - vals[i-1]) / vals[i-1] for i in range(1, len(vals)) if vals[i-1] > 0]
    if not daily_rets:
        return {}

    mean_daily = sum(daily_rets) / len(daily_rets)
    neg_rets = [r for r in daily_rets if r < 0]
    downside_dev = (sum(r**2 for r in neg_rets) / len(neg_rets)) ** 0.5 if neg_rets else 0.001
    sortino = (mean_daily * 252) / (downside_dev * (252 ** 0.5))

    peak, max_dd = vals[0], 0.0
    for v in vals:
        peak = max(peak, v)
        dd = (peak - v) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

    port_ret = (vals[-1] - vals[0]) / vals[0] if vals[0] > 0 else 0

    # ARR (Annualized Return Rate)
    n_days = max(len(vals), 1)
    arr = ((1 + port_ret) ** (252 / n_days) - 1) if port_ret > -1 else -1.0

    # Win Rate
    win_rate = len([r for r in daily_rets if r >= 0]) / len(daily_rets) * 100 if daily_rets else 0

    spy_ret = None
    if spy_history and len(spy_history) >= 2:
        s_vals = [s["equity"] for s in spy_history]
        spy_ret = (s_vals[-1] - s_vals[0]) / s_vals[0] if s_vals[0] > 0 else 0

    qqq_ret = None
    if qqq_history and len(qqq_history) >= 2:
        q_vals = [q["equity"] for q in qqq_history]
        qqq_ret = (q_vals[-1] - q_vals[0]) / q_vals[0] if q_vals[0] > 0 else 0

    return {
        "sortino": round(sortino, 2),
        "max_dd": round(max_dd * 100, 2),
        "port_ret_1m": round(port_ret * 100, 2),
        "arr": round(arr * 100, 2),
        "win_rate": round(win_rate, 1),
        "spy_ret_1m": round(spy_ret * 100, 2) if spy_ret is not None else None,
        "qqq_ret_1m": round(qqq_ret * 100, 2) if qqq_ret is not None else None,
        "alpha_1m": round((port_ret - spy_ret) * 100, 2) if spy_ret is not None else None,
        "alpha_vs_qqq": round((port_ret - qqq_ret) * 100, 2) if qqq_ret is not None else None,
    }


# ─── TAB: RESUMEN ─────────────────────────────────────────────────────────────

def _tab_resumen(equity, initial, regime, vix, wti, gold, dxy,
                 history, spy_history, signals_data, metrics, age_hours,
                 perf_data=None, qqq_history=None):
    pnl     = equity - initial
    pnl_pct = (pnl / initial * 100) if initial else 0
    pc      = _c(pnl)
    pbg     = _bg(pnl)
    rl, rc  = _regime(regime)
    vl, vc  = _vix_info(vix)

    macro   = signals_data.get("macro", {})
    port    = signals_data.get("portfolio", {})
    sharpe  = port.get("sharpe", 0) or 0
    ret_exp = (port.get("exp_return", 0) or 0) * 100
    vol     = (port.get("volatility", 0) or 0) * 100
    gen_at  = signals_data.get("generated_at", "")[:16].replace("T", " ")

    # Freshness banner
    fresh_html = ""
    if age_hours >= 24:
        fresh_html = '<div class="fresh-banner fresh-stale">⚠️ DATOS DESACTUALIZADOS — Senales de hace mas de 24h. El analyst no ha corrido hoy.</div>'
    elif age_hours >= 8:
        fresh_html = f'<div class="fresh-banner fresh-warn">⚠️ Senales de hace {age_hours:.0f}h — El dashboard puede no reflejar la sesion de hoy.</div>'

    # Hero KPI row
    spy_badge = ""
    if metrics.get("spy_ret_1m") is not None:
        spy_badge = f'&nbsp;&middot;&nbsp;<span style="color:#7d8590">SPY {_pct(metrics["spy_ret_1m"])}</span>'

    arr_v     = metrics.get("arr", 0) or 0
    win_rate  = metrics.get("win_rate", 0) or 0

    kpis = f"""
<div class="kpi-row">
  <div class="kpi kpi-hero" data-countup="{equity:.2f}">
    <div class="kpi-lbl">Patrimonio Total</div>
    <div class="kpi-val kpi-hero-val" style="color:{pc}" id="kpi-equity">{_usd(equity)}</div>
    <div class="kpi-tag" style="background:{pbg};color:{pc}">{_usd(pnl)} &nbsp; {_pct(pnl_pct)} total{spy_badge}</div>
  </div>
  <div class="kpi">
    <div class="kpi-lbl">Regimen de Mercado</div>
    <div class="kpi-val" style="color:{rc}">{rl}</div>
    <div class="kpi-sub">Capital base {_usd(initial)}</div>
  </div>
  <div class="kpi">
    <div class="kpi-lbl">VIX — Volatilidad</div>
    <div class="kpi-val" style="color:{vc}">{vix:.1f}</div>
    <div class="kpi-sub">{vl}</div>
  </div>
  <div class="kpi">
    <div class="kpi-lbl">Sharpe del Portfolio</div>
    <div class="kpi-val" style="color:{'#3fb950'if sharpe>0.5 else'#d29922'if sharpe>0 else'#f85149'}">{sharpe:.2f}</div>
    <div class="kpi-sub">Retorno esperado {_pct(ret_exp)}</div>
  </div>
  <div class="kpi">
    <div class="kpi-lbl">ARR (Anualizado)</div>
    <div class="kpi-val" style="color:{'#3fb950'if arr_v>10 else'#d29922'if arr_v>0 else'#f85149'}">{_pct(arr_v)}</div>
    <div class="kpi-sub">Retorno anualizado estimado</div>
  </div>
  <div class="kpi">
    <div class="kpi-lbl">Win Rate</div>
    <div class="kpi-val" style="color:{'#3fb950'if win_rate>55 else'#d29922'if win_rate>45 else'#f85149'}">{win_rate:.1f}%</div>
    <div class="kpi-sub">Dias con P&L positivo</div>
  </div>
  <div class="kpi">
    <div class="kpi-lbl">Petroleo WTI</div>
    <div class="kpi-val">{_usd(wti)}</div>
    <div class="kpi-sub">Oro: {_usd(gold)} &middot; DXY: {dxy:.1f}</div>
  </div>
</div>"""

    # Advanced metrics row
    adv_kpis = ""
    if metrics:
        sortino_v = metrics.get("sortino", 0) or 0
        max_dd_v  = metrics.get("max_dd", 0) or 0
        alpha_v   = metrics.get("alpha_1m")
        alpha_qqq = metrics.get("alpha_vs_qqq")
        spy_r     = metrics.get("spy_ret_1m")
        qqq_r     = metrics.get("qqq_ret_1m")
        port_r    = metrics.get("port_ret_1m", 0) or 0

        alpha_html = (
            f'<div class="kpi"><div class="kpi-lbl">Alpha vs SPY (1M)</div>'
            f'<div class="kpi-val" style="color:{_c(alpha_v)}">{_pct(alpha_v)}</div>'
            f'<div class="kpi-sub">Portfolio {_pct(port_r)} · SPY {_pct(spy_r)}</div></div>'
        ) if alpha_v is not None else ""

        qqq_html = (
            f'<div class="kpi"><div class="kpi-lbl">Alpha vs QQQ (1M)</div>'
            f'<div class="kpi-val" style="color:{_c(alpha_qqq)}">{_pct(alpha_qqq)}</div>'
            f'<div class="kpi-sub">QQQ {_pct(qqq_r)}</div></div>'
        ) if alpha_qqq is not None else ""

        adv_kpis = f"""
<div class="kpi-row kpi-row-adv">
  <div class="kpi">
    <div class="kpi-lbl">Sortino Ratio (1M)</div>
    <div class="kpi-val" style="color:{'#3fb950'if sortino_v>1 else'#d29922'if sortino_v>0 else'#f85149'}">{sortino_v:.2f}</div>
    <div class="kpi-sub">Riesgo / retorno ajustado baja</div>
  </div>
  <div class="kpi">
    <div class="kpi-lbl">Max Drawdown (1M)</div>
    <div class="kpi-val" style="color:{'#3fb950'if max_dd_v<3 else'#d29922'if max_dd_v<7 else'#f85149'}">-{max_dd_v:.2f}%</div>
    <div class="kpi-sub">Caida maxima desde pico</div>
  </div>
  {alpha_html}
  {qqq_html}
</div>"""

    eq_chart = _equity_chart(history, spy_history, qqq_history=qqq_history)
    cal      = _pnl_calendar(history)
    ts_info  = f'<p class="ts">Senales actualizadas: {gen_at} &nbsp;|&nbsp; Paper Trading</p>' if gen_at else ""

    return f"""
<div class="tab-content" id="tab-resumen">
  {fresh_html}
  {ts_info}
  {kpis}
  {adv_kpis}
  <div class="section-gap"></div>
  {eq_chart}
  <div class="section-gap"></div>
  {cal}
  <div class="section-gap"></div>
  {_perf_chart(perf_data)}
</div>"""


def _equity_chart(history, spy_history=None, qqq_history=None):
    if len(history) < 2:
        return '<div class="card"><p class="muted" style="padding:40px;text-align:center">Sin historial de patrimonio disponible todavia.</p></div>'

    labels = [datetime.fromtimestamp(h["ts"]).strftime("%d %b") for h in history]
    vals   = [h["equity"] for h in history]
    first  = vals[0] if vals[0] else 1.0

    # Normalizar a % de retorno desde el primer punto
    norm_vals = [round((v - first) / first * 100, 3) for v in vals]
    total_pct_v = norm_vals[-1]
    color = _c(total_pct_v)

    spy_dataset = ""
    spy_legend  = ""
    if spy_history and len(spy_history) >= 2:
        spy_raw  = [s["equity"] for s in spy_history]
        spy_base = spy_raw[0] if spy_raw[0] else 1.0
        spy_norm = [round((v - spy_base) / spy_base * 100, 3) for v in spy_raw]
        spy_last = spy_norm[-1]
        spy_dataset = f""",
    {{data:{json.dumps(spy_norm)},label:'SPY',
      borderColor:'#58a6ff',backgroundColor:'transparent',fill:false,
      tension:.4,pointRadius:0,borderWidth:1.5,borderDash:[5,3]}}"""
        spy_legend = f'<span style="font-size:.78rem;color:#58a6ff">&#9135;&#9135; SPY {_pct(spy_last)}</span>'

    qqq_dataset = ""
    qqq_legend  = ""
    if qqq_history and len(qqq_history) >= 2:
        qqq_raw  = [q["equity"] for q in qqq_history]
        qqq_base = qqq_raw[0] if qqq_raw[0] else 1.0
        qqq_norm = [round((v - qqq_base) / qqq_base * 100, 3) for v in qqq_raw]
        qqq_last = qqq_norm[-1]
        qqq_dataset = f""",
    {{data:{json.dumps(qqq_norm)},label:'QQQ',
      borderColor:'#e3b341',backgroundColor:'transparent',fill:false,
      tension:.4,pointRadius:0,borderWidth:1.5,borderDash:[3,3]}}"""
        qqq_legend = f'<span style="font-size:.78rem;color:#e3b341">&#9135;&#9135; QQQ {_pct(qqq_last)}</span>'

    return f"""
<div class="card">
  <div class="card-head">
    <div>
      <div class="card-title">Retorno Acumulado — % vs benchmarks</div>
      <div class="card-sub" style="display:flex;gap:16px;align-items:center;flex-wrap:wrap">
        <span><span style="color:{color};font-weight:700;font-size:.9rem">{_pct(total_pct_v)}</span> Portfolio</span>
        {spy_legend}
        {qqq_legend}
      </div>
    </div>
    <div class="pill" style="color:{color}">{_usd(vals[-1])}</div>
  </div>
  <canvas id="eqChart" height="85"></canvas>
</div>
<script>
(function(){{
  const ctx = document.getElementById('eqChart');
  const g = ctx.getContext('2d').createLinearGradient(0,0,0,300);
  g.addColorStop(0,'{color}33'); g.addColorStop(1,'{color}00');
  window._charts = window._charts||{{}};
  window._charts.eq = new Chart(ctx,{{
    type:'line',
    data:{{
      labels:{json.dumps(labels)},
      datasets:[
        {{data:{json.dumps(norm_vals)},label:'Portfolio',
          borderColor:'{color}',backgroundColor:g,fill:true,tension:.4,
          pointRadius:2,pointHoverRadius:7,pointBackgroundColor:'{color}',borderWidth:2.5}}
        {spy_dataset}
        {qqq_dataset}
      ]
    }},
    options:{{
      animation:{{duration:900,easing:'easeInOutQuart'}},
      interaction:{{mode:'index',intersect:false}},
      plugins:{{
        legend:{{display:false}},
        tooltip:{{
          backgroundColor:'#161b22',borderColor:'#30363d',borderWidth:1,
          titleColor:'#7d8590',bodyColor:'#e6edf3',padding:12,
          callbacks:{{label:c=>` ${{c.dataset.label}}: ${{c.parsed.y>=0?'+':''}}${{c.parsed.y.toFixed(2)}}%`}}
        }}
      }},
      scales:{{
        x:{{ticks:{{color:'#7d8590',maxTicksLimit:10,font:{{size:11}}}},grid:{{color:'#21262d'}}}},
        y:{{
          ticks:{{color:'#7d8590',callback:v=>(v>=0?'+':'')+v.toFixed(1)+'%',font:{{size:11}}}},
          grid:{{color:'#21262d'}},
          afterDataLimits(scale){{scale.min=Math.min(scale.min,-0.5);}}
        }}
      }}
    }}
  }});
}})();
</script>"""


def _pnl_calendar(history):
    if len(history) < 2:
        return ""
    import calendar as cm
    from datetime import date

    daily = {}
    for i in range(1, len(history)):
        pe = history[i-1]["equity"]
        ce = history[i]["equity"]
        d  = datetime.fromtimestamp(history[i]["ts"]).date()
        pnl = ce - pe
        pnl_pct = (pnl / pe * 100) if pe else 0
        daily[d] = {"pnl": pnl, "pct": pnl_pct}

    if not daily:
        return ""

    last = max(daily.keys())
    yr, mo = last.year, last.month
    month_name = ["Enero","Febrero","Marzo","Abril","Mayo","Junio",
                  "Julio","Agosto","Septiembre","Octubre","Noviembre","Diciembre"][mo-1]

    month_d = {d:v for d,v in daily.items() if d.year==yr and d.month==mo}
    total   = sum(v["pnl"] for v in month_d.values())
    wins    = sum(1 for v in month_d.values() if v["pnl"]>=0)
    losses  = len(month_d) - wins
    best    = max(month_d.values(), key=lambda v: v["pnl"], default=None)
    worst   = min(month_d.values(), key=lambda v: v["pnl"], default=None)

    def cell_style(pnl):
        if pnl > 0:
            i = min(pnl/250, 1.0)
            r = int(10 + 30*i); g = int(55 + 130*i); b = int(25 + 30*i)
            return f"background:rgb({r},{g},{b})", "#3fb950" if i>.4 else "#86efac"
        elif pnl < 0:
            i = min(abs(pnl)/250, 1.0)
            r = int(55 + 170*i); g = int(15 + 20*i); b = int(15 + 20*i)
            return f"background:rgb({r},{g},{b})", "#f85149" if i>.4 else "#fca5a5"
        return "background:var(--s2)", "#7d8590"

    n_days = cm.monthrange(yr, mo)[1]
    start  = date(yr, mo, 1).weekday()
    rows   = ""
    day_n  = 1 - start

    while day_n <= n_days:
        rows += "<tr>"
        for dow in range(7):
            if day_n < 1 or day_n > n_days:
                rows += '<td class="cal-empty"></td>'
            else:
                d    = date(yr, mo, day_n)
                data = month_d.get(d)
                if data:
                    bg, tc2 = cell_style(data["pnl"])
                    s = "+" if data["pnl"]>=0 else ""
                    rows += f"""<td class="cal-cell" style="{bg}">
  <span class="cal-n">{day_n}</span>
  <span class="cal-p" style="color:{tc2}">{s}${data['pnl']:,.0f}</span>
  <span class="cal-q" style="color:{tc2}">{s}{data['pct']:.1f}%</span>
</td>"""
                elif dow >= 5:
                    rows += f'<td class="cal-cell cal-wk"><span class="cal-n">{day_n}</span></td>'
                else:
                    rows += f'<td class="cal-cell cal-nd"><span class="cal-n">{day_n}</span><span class="cal-q">—</span></td>'
            day_n += 1
        rows += "</tr>"

    bh = f'<b style="color:#3fb950">+${best["pnl"]:,.0f}</b>' if best else "—"
    wh = f'<b style="color:#f85149">${worst["pnl"]:,.0f}</b>' if worst else "—"

    return f"""
<div class="card">
  <div class="card-head">
    <div>
      <div class="card-title">Resultados Diarios — {month_name} {yr}</div>
      <div class="card-sub">{wins} dias positivos &middot; {losses} negativos &middot; Mejor: {bh} &middot; Peor: {wh}</div>
    </div>
    <div class="pill" style="color:{_c(total)};font-size:1rem;font-weight:700">{"+" if total>=0 else ""}${total:,.0f}</div>
  </div>
  <div class="cal-wrap">
    <table class="cal-table">
      <thead><tr>{"".join(f'<th class="cal-th">{d}</th>' for d in ["Lun","Mar","Mie","Jue","Vie","Sab","Dom"])}</tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
</div>"""


# ─── TAB: POSICIONES ──────────────────────────────────────────────────────────

def _tab_posiciones(positions):
    if not positions:
        return """
<div class="tab-content" id="tab-posiciones">
  <div class="card"><p class="muted" style="padding:60px;text-align:center;font-size:1rem">
    Sin posiciones abiertas en este momento.
  </p></div>
</div>"""

    COLORS = ["#58a6ff","#3fb950","#d29922","#f85149","#bc8cff",
              "#ffa657","#79c0ff","#56d364","#ff7b72","#d2a8ff"]

    # Separar posiciones activas de expiradas/sin valor
    active_pos  = [p for p in positions if (p.market_value or 0) > 1.0]
    expired_pos = [p for p in positions if (p.market_value or 0) <= 1.0]

    # Usar solo activas para el cuerpo principal
    positions = active_pos if active_pos else positions

    tickers  = [p.ticker for p in positions]
    values   = [round(p.market_value, 2) for p in positions]
    colors   = [COLORS[i % len(COLORS)] for i in range(len(positions))]
    total_mv  = sum(values)
    total_pnl = sum(p.unrealized_pl for p in positions)
    tp_c      = _c(total_pnl)

    cards = ""
    for i, p in enumerate(positions):
        cost    = (p.avg_price or 0) * (p.qty or 0)
        pnl     = p.unrealized_pl
        pp      = (pnl / cost * 100) if cost else 0
        pc      = _c(pnl)
        col     = colors[i]
        bar_w   = min(abs(pp) * 5, 100)
        alloc_p = (p.market_value / total_mv * 100) if total_mv else 0
        pnl_arrow = "▲" if pnl >= 0 else "▼"
        cards += f"""
<div class="pos-card" style="border-top:3px solid {col}">
  <div class="pos-top">
    <div>
      <div class="pos-ticker" style="color:{col}">{p.ticker}</div>
      <div class="pos-sub">{alloc_p:.1f}% del portfolio &middot; {p.qty:.4f} acc.</div>
    </div>
    <div style="text-align:right">
      <div class="pos-pnl" style="color:{pc}">{pnl_arrow} {_usd(abs(pnl))}</div>
      <div class="pos-pnl-pct" style="color:{pc}">{_pct(pp,1)}</div>
    </div>
  </div>
  <div class="pos-bar-track"><div style="width:{bar_w:.0f}%;height:100%;background:{pc};border-radius:2px;opacity:.8"></div></div>
  <div class="pos-meta">
    <span>Entrada <b>{_usd(p.avg_price)}</b></span>
    <span>Valor actual <b>{_usd(p.market_value)}</b></span>
    <span>P&L <b style="color:{pc}">{"+" if pnl>=0 else ""}{_usd(pnl)}</b></span>
  </div>
</div>"""

    # Sección de posiciones expiradas/sin valor (colapsable)
    expired_html = ""
    if expired_pos:
        expired_rows = ""
        for p in expired_pos:
            pnl = p.unrealized_pl or 0
            pc  = _c(pnl)
            expired_rows += f"""
<tr>
  <td><span style="color:#8b949e;font-weight:600">{p.ticker}</span></td>
  <td class="muted">{_usd(p.avg_price or 0)}</td>
  <td><span style="color:#f85149">$0.00</span></td>
  <td><span style="color:{pc};font-weight:600">{"+" if pnl>=0 else ""}{_usd(pnl)}</span></td>
  <td><span style="font-size:.72rem;background:#21262d;color:#8b949e;padding:2px 8px;border-radius:10px">Expirada</span></td>
</tr>"""
        expired_html = f"""
<div class="section-gap"></div>
<details style="cursor:pointer">
  <summary style="font-size:.82rem;color:#7d8590;padding:8px 0;user-select:none">
    &#9660; {len(expired_pos)} posicion(es) expirada(s) / sin valor (P&L cerrado)
  </summary>
  <div class="card" style="margin-top:8px">
    <table>
      <thead><tr><th>Ticker</th><th>Entrada</th><th>Valor actual</th><th>P&L</th><th>Estado</th></tr></thead>
      <tbody>{expired_rows}</tbody>
    </table>
  </div>
</details>"""

    return f"""
<div class="tab-content" id="tab-posiciones">
  <div class="pos-summary">
    <div class="kpi">
      <div class="kpi-lbl">Posiciones activas</div>
      <div class="kpi-val">{len(positions)}</div>
    </div>
    <div class="kpi">
      <div class="kpi-lbl">Valor de mercado total</div>
      <div class="kpi-val">{_usd(total_mv)}</div>
    </div>
    <div class="kpi">
      <div class="kpi-lbl">P&amp;L no realizado</div>
      <div class="kpi-val" style="color:{tp_c}">{"+" if total_pnl>=0 else ""}{_usd(total_pnl)}</div>
    </div>
  </div>
  <div class="pos-layout">
    <div class="pos-chart-col">
      <div class="card-title" style="margin-bottom:12px">Distribucion</div>
      <canvas id="allocChart" width="220" height="220"></canvas>
      <div id="allocLegend" class="alloc-legend"></div>
    </div>
    <div class="pos-cards-col">
      {cards}
    </div>
  </div>
  {expired_html}
</div>
<script>
(function(){{
  const labels={json.dumps(tickers)};
  const values={json.dumps(values)};
  const colors={json.dumps(colors)};
  window._charts = window._charts||{{}};
  window._charts.alloc = new Chart(document.getElementById('allocChart'),{{
    type:'doughnut',
    data:{{labels,datasets:[{{data:values,backgroundColor:colors.map(c=>c+'cc'),
      borderWidth:2,borderColor:'#0d1117',hoverBorderColor:'#fff'}}]}},
    options:{{
      cutout:'72%',
      animation:{{duration:900,easing:'easeInOutQuart'}},
      plugins:{{
        legend:{{display:false}},
        tooltip:{{backgroundColor:'#161b22',borderColor:'#30363d',borderWidth:1,
          titleColor:'#7d8590',bodyColor:'#e6edf3',padding:10,
          callbacks:{{label:c=>' $'+c.parsed.toLocaleString('es-AR',{{minimumFractionDigits:2}})}}}}
      }}
    }}
  }});
  const leg=document.getElementById('allocLegend');
  const tot=values.reduce((a,b)=>a+b,0);
  labels.forEach((l,i)=>{{
    const pct=(values[i]/tot*100).toFixed(1);
    leg.innerHTML+=`<div class="leg-row"><span class="leg-dot" style="background:${{colors[i]}}"></span>${{l}}<span class="muted" style="margin-left:auto">${{pct}}%</span></div>`;
  }});
}})();
</script>"""


def _perf_chart(perf_data: dict | None) -> str:
    """Gráfico de barras agrupadas: portfolio vs SPY semana a semana."""
    weeks = (perf_data or {}).get("weeks", [])
    if not weeks:
        return '<div class="card"><div class="card-head"><div><div class="card-title">Performance Semanal vs SPY</div><div class="card-sub">Disponible tras el primer rebalanceo del viernes</div></div></div><p class="muted" style="padding:8px 0 12px">Sin historial de performance semanal todavia.</p></div>'
    if not weeks:
        return '<div class="card"><p class="muted" style="padding:20px">Sin historial de performance semanal todavia. Disponible tras el primer rebalanceo del viernes.</p></div>'

    labels      = [w.get("date", "")[-5:].replace("-", "/") for w in weeks]
    port_vals   = [w.get("portfolio_pct") for w in weeks]
    spy_vals    = [w.get("spy_pct") for w in weeks]
    alpha_vals  = [w.get("alpha_pct") for w in weeks]

    # Stats
    valid_alpha = [a for a in alpha_vals if a is not None]
    cum_alpha   = round(sum(valid_alpha), 2) if valid_alpha else None
    pos_weeks   = sum(1 for a in valid_alpha if a > 0)
    total_weeks = len(valid_alpha)

    cum_color = "#3fb950" if (cum_alpha or 0) >= 0 else "#f85149"
    cum_sign  = "+" if (cum_alpha or 0) >= 0 else ""
    stats_html = ""
    if cum_alpha is not None:
        win_rate = pos_weeks / total_weeks * 100 if total_weeks else 0
        stats_html = f"""
<div style="display:flex;gap:24px;padding:10px 0 4px;flex-wrap:wrap">
  <div><span class="muted" style="font-size:.72rem">Alpha acumulado</span>
    <span style="color:{cum_color};font-weight:700;margin-left:8px">{cum_sign}{cum_alpha:.2f}%</span></div>
  <div><span class="muted" style="font-size:.72rem">Semanas positivas</span>
    <span style="color:#e6edf3;font-weight:700;margin-left:8px">{pos_weeks}/{total_weeks}</span></div>
</div>"""

    # Chart.js data — null-safe
    port_js = json.dumps([round(v, 2) if v is not None else None for v in port_vals])
    spy_js  = json.dumps([round(v, 2) if v is not None else None for v in spy_vals])

    return f"""
<div class="card">
  <div class="card-head">
    <div>
      <div class="card-title">Performance Semanal vs SPY</div>
      <div class="card-sub">Retorno semanal del portfolio comparado con el benchmark</div>
    </div>
  </div>
  {stats_html}
  <canvas id="perfChart" height="100"></canvas>
</div>
<script>
(function(){{
  window._charts=window._charts||{{}};
  window._charts.perf=new Chart(document.getElementById('perfChart'),{{
    type:'bar',
    data:{{
      labels:{json.dumps(labels)},
      datasets:[
        {{label:'Portfolio',data:{port_js},backgroundColor:'#1f6feb88',
          borderColor:'#1f6feb',borderWidth:1,borderRadius:4,borderSkipped:false}},
        {{label:'SPY',data:{spy_js},backgroundColor:'#7d859044',
          borderColor:'#7d8590',borderWidth:1,borderRadius:4,borderSkipped:false}}
      ]
    }},
    options:{{
      animation:{{duration:600}},
      plugins:{{
        legend:{{labels:{{color:'#7d8590',font:{{size:11}}}}}},
        tooltip:{{backgroundColor:'#161b22',borderColor:'#30363d',borderWidth:1,
          titleColor:'#7d8590',bodyColor:'#e6edf3',padding:10,
          callbacks:{{label:c=>` ${{c.dataset.label}}: ${{c.parsed.y!=null?c.parsed.y.toFixed(2):'N/A'}}%`}}}}
      }},
      scales:{{
        x:{{ticks:{{color:'#7d8590',font:{{size:10}}}},grid:{{display:false}}}},
        y:{{ticks:{{color:'#7d8590',callback:v=>v+'%',font:{{size:10}}}},grid:{{color:'#21262d'}}}}
      }}
    }}
  }});
}})();
</script>"""


def _ws_block(ws: dict | None) -> str:
    if not ws:
        return ""
    rec = ws.get("recommendation", "")
    pt_pct = ws.get("price_target_pct", 0) or 0
    val = ws.get("valuation", "")
    thesis_ws = _esc(ws.get("thesis", "") or "")
    catalysts = ws.get("catalysts", []) or []
    risks = ws.get("risks", []) or []

    rec_color = {"BUY": "#3fb950", "SELL": "#f85149", "HOLD": "#d29922"}.get(rec.upper(), "#8b949e")
    pt_sign = "+" if pt_pct >= 0 else ""
    pt_color = "#3fb950" if pt_pct >= 0 else "#f85149"

    cat_html = "".join(f'<li>{_esc(c)}</li>' for c in catalysts[:3]) if catalysts else ""
    risk_html = "".join(f'<li>{_esc(r)}</li>' for r in risks[:3]) if risks else ""

    return f"""
<div class="ws-block">
  <div class="ws-header">
    <span class="ws-label">WALL ST ANALYSIS</span>
    <span class="ws-rec" style="color:{rec_color};border-color:{rec_color}55">{rec}</span>
    <span class="ws-pt" style="color:{pt_color}">&nbsp;PT {pt_sign}{pt_pct:.1f}%</span>
    <span class="ws-val muted">&nbsp;&middot;&nbsp;{_esc(val)}</span>
  </div>
  {f'<p class="ws-thesis">{thesis_ws}</p>' if thesis_ws else ''}
  <div class="ws-lists">
    {f'<div class="ws-col"><div class="ws-col-h">Catalizadores</div><ul>{cat_html}</ul></div>' if cat_html else ''}
    {f'<div class="ws-col"><div class="ws-col-h">Riesgos</div><ul>{risk_html}</ul></div>' if risk_html else ''}
  </div>
</div>"""


# ─── TAB: SEÑALES ─────────────────────────────────────────────────────────────

def _tab_senales(signals_data):
    if not signals_data:
        return '<div class="tab-content" id="tab-senales"><div class="card"><p class="muted" style="padding:40px">Sin senales disponibles.</p></div></div>'

    gen_at = signals_data.get("generated_at","")[:16].replace("T"," ")
    body   = ""

    for bucket in ("long_term","short_term","options_book","hedge_book"):
        items = signals_data.get(bucket, [])
        if not items:
            continue
        slv_label, slv_color = _sleeve(bucket)
        cards = ""
        for s in items:
            th     = s.get("thesis", {})
            q      = th.get("quant", {})
            risk   = th.get("risk", {})
            conv   = th.get("conviction","?")
            cl, cc = _conv(conv)
            text   = _esc(th.get("thesis_text","Sin descripcion."))
            price  = s.get("price",0) or 0
            sl     = s.get("stop_loss")
            tp     = s.get("take_profit")
            sharpe = q.get("sharpe",0) or 0
            alpha  = (q.get("alpha_jensen",0) or 0)*100
            beta   = q.get("beta",0) or 0
            alloc  = risk.get("dollars_allocated",0) or 0
            ticker = s.get("ticker","?")
            cp     = {"ALTA":88,"MEDIA":52,"BAJA":22}.get(conv,40)
            uid    = f"t_{ticker}_{bucket}"

            sl_h = f'<span style="color:#f85149;font-weight:700">{_usd(sl)}</span>' if sl else '<span class="muted">—</span>'
            tp_h = f'<span style="color:#3fb950;font-weight:700">{_usd(tp)}</span>' if tp else '<span class="muted">—</span>'

            cards += f"""
<div class="sig-card" onclick="toggleSig('{uid}')">
  <div class="sig-head">
    <div class="sig-left">
      <span class="sig-ticker">{ticker}</span>
      <span class="conv-badge" style="background:{cc}22;color:{cc};border:1px solid {cc}55">{cl}</span>
    </div>
    <div class="sig-right">
      <span class="muted" style="font-size:.78rem">{_usd(price)}</span>
      <span class="sig-alloc">{_usd(alloc)}</span>
      <span class="sig-arrow" id="arrow_{uid}">&#9660;</span>
    </div>
  </div>
  <div class="sig-pills">
    <div class="spill"><div class="spill-l">Sharpe</div><div class="spill-v" style="color:#58a6ff">{sharpe:.2f}</div></div>
    <div class="spill"><div class="spill-l">Alfa Jensen</div><div class="spill-v" style="color:{'#3fb950'if alpha>=0 else'#f85149'}">{_pct(alpha)}</div></div>
    <div class="spill"><div class="spill-l">Beta</div><div class="spill-v">{beta:.2f}</div></div>
    <div class="spill"><div class="spill-l">Stop Loss</div><div class="spill-v">{sl_h}</div></div>
    <div class="spill"><div class="spill-l">Take Profit</div><div class="spill-v">{tp_h}</div></div>
  </div>
  <div class="conv-track"><div class="conv-fill" style="width:{cp}%;background:{cc}88"></div></div>
  <div class="sig-thesis" id="{uid}">
    <p>{text}</p>
    {_ws_block(th.get("wall_street"))}
  </div>
</div>"""

        body += f"""
<div class="sleeve-block">
  <div class="sleeve-header" style="border-left:3px solid {slv_color}">
    <span class="sleeve-label" style="color:{slv_color}">{slv_label}</span>
    <span class="sleeve-count muted">{len(items)} {"senal" if len(items)==1 else "senales"}</span>
  </div>
  <div class="sig-grid">
    {cards}
  </div>
</div>"""

    return f"""
<div class="tab-content" id="tab-senales">
  <p class="ts" style="margin-bottom:16px">Ultima actualizacion: {gen_at} &nbsp;&middot;&nbsp; Clic en cada senal para ver el analisis completo</p>
  {body}
</div>"""


# ─── TAB: MERCADO ─────────────────────────────────────────────────────────────

def _discovery_section(disc_data: dict | None) -> str:
    """Candidatos de Discovery fuera del universo actual."""
    if disc_data is None:
        return '<div class="card"><p class="muted" style="padding:20px">Discovery corre los viernes. Sin datos todavia.</p></div>'

    # Freshness check
    gen_at_str = disc_data.get("generated_at", "")
    stale = False
    gen_label = ""
    if gen_at_str:
        try:
            from datetime import timezone as _tz
            gen_dt = datetime.fromisoformat(gen_at_str)
            if gen_dt.tzinfo is None:
                gen_dt = gen_dt.replace(tzinfo=timezone.utc)
            age_d = (datetime.now(timezone.utc) - gen_dt).days
            stale = age_d > 8
            gen_label = gen_at_str[:10]
        except Exception:
            pass

    candidates    = disc_data.get("candidates", [])
    repeated      = set(disc_data.get("repeated_alerts", []))
    n_scanned     = disc_data.get("n_scanned", 0)
    regime        = disc_data.get("regime", "")

    stale_banner = '<div class="fresh-banner fresh-warn" style="margin-bottom:12px">Datos de discovery desactualizados (>7 dias). Corre el proximo viernes.</div>' if stale else ""

    cards_html = ""
    for c in candidates:
        ticker   = c.get("ticker", "?")
        prior    = c.get("prioridad", "BAJA").upper()
        razon    = _esc(c.get("razon", ""))
        riesgo   = _esc(c.get("riesgo", ""))
        is_rep   = ticker in repeated
        prior_color = {"ALTA": "#3fb950", "MEDIA": "#d29922"}.get(prior, "#7d8590")
        rep_badge = '<span class="disc-rep">2da semana</span>' if is_rep else ""

        cards_html += f"""
<div class="disc-card">
  <div class="disc-head">
    <span class="disc-ticker">{ticker}</span>
    <span class="disc-prior" style="background:{prior_color}22;color:{prior_color};border-color:{prior_color}55">{prior}</span>
    {rep_badge}
  </div>
  <p class="disc-razon">{razon}</p>
  {f'<p class="disc-riesgo">⚠ {riesgo}</p>' if riesgo else ''}
</div>"""

    if not cards_html:
        cards_html = '<p class="muted">Sin candidatos esta semana.</p>'

    meta = f"{n_scanned} activos escaneados" + (f" · régimen {regime}" if regime else "") + (f" · {gen_label}" if gen_label else "")

    return f"""
<div class="card">
  <div class="card-head">
    <div>
      <div class="card-title">Oportunidades fuera del universo</div>
      <div class="card-sub">{meta}</div>
    </div>
  </div>
  {stale_banner}
  <div class="disc-grid">{cards_html}</div>
</div>"""


def _tab_mercado(signals_data, discovery_data=None):
    radar   = signals_data.get("radar", {})
    entries = radar.get("entries", [])
    n_up    = radar.get("n_up", 0)
    n_down  = radar.get("n_down", 0)
    winner  = radar.get("biggest_winner","—")
    loser   = radar.get("biggest_loser","—")
    n_total = len(entries)

    # Sector rotation chart
    sector_html = ""
    try:
        from alpha_agent.macro.sector_rotation import get_top_sectors
        tops = get_top_sectors(n=10)
        if tops:
            labels  = [s for s,_ in tops]
            values  = [round(v*100,2) for _,v in tops]
            bcolors = ["#3fb950" if v>=0 else "#f85149" for v in values]
            sector_html = f"""
<div class="card">
  <div class="card-head">
    <div>
      <div class="card-title">Rotacion Sectorial</div>
      <div class="card-sub">Impulso de precio 1–3 meses por sector</div>
    </div>
  </div>
  <canvas id="sectorChart" height="115"></canvas>
</div>
<script>
(function(){{
  window._charts=window._charts||{{}};
  window._charts.sector=new Chart(document.getElementById('sectorChart'),{{
    type:'bar',
    data:{{labels:{json.dumps(labels)},datasets:[{{data:{json.dumps(values)},
      backgroundColor:{json.dumps(bcolors)},borderRadius:6,borderSkipped:false}}]}},
    options:{{
      animation:{{duration:700}},
      plugins:{{legend:{{display:false}},tooltip:{{backgroundColor:'#161b22',borderColor:'#30363d',
        borderWidth:1,titleColor:'#7d8590',bodyColor:'#e6edf3',padding:10,
        callbacks:{{label:c=>' '+c.parsed.y.toFixed(2)+'%'}}}}}},
      scales:{{
        x:{{ticks:{{color:'#7d8590',font:{{size:11}}}},grid:{{display:false}}}},
        y:{{ticks:{{color:'#7d8590',callback:v=>v+'%',font:{{size:11}}}},grid:{{color:'#21262d'}}}}
      }}
    }}
  }});
}})();
</script>"""
        else:
            sector_html = '<div class="card"><p class="muted" style="padding:20px">Sin datos de rotacion sectorial.</p></div>'
    except Exception as ex:
        sector_html = f'<div class="card"><p class="muted" style="padding:20px">Rotacion sectorial no disponible: {_esc(str(ex)[:80])}</p></div>'

    # Radar table — ALL entries
    radar_rows = ""
    for e in entries:
        t    = e.get("ticker","")
        move = (e.get("pct_1d") or e.get("move_pct") or 0) * 100
        news = _esc((e.get("headline") or e.get("top_news", ""))[:100])
        act  = _esc(e.get("action") or e.get("bot_action", ""))
        mc   = _c(move)
        arr  = "▲" if move>=0 else "▼"
        radar_rows += f"""
<tr>
  <td><span class="rt-ticker" style="color:#e6edf3">{t}</span></td>
  <td><span style="color:{mc};font-weight:700">{arr} {abs(move):.1f}%</span></td>
  <td class="muted" style="font-size:.8rem;max-width:320px">{news}</td>
  <td><span class="muted" style="font-size:.75rem">{act}</span></td>
</tr>"""

    if not radar_rows:
        radar_rows = '<tr><td colspan="4" class="muted" style="padding:20px;text-align:center">Sin datos de radar disponibles</td></tr>'

    radar_html = f"""
<div class="card">
  <div class="card-head">
    <div>
      <div class="card-title">Radar del Universo — {n_total} Activos</div>
      <div class="card-sub">
        <span style="color:#3fb950">&#9650; {n_up} al alza</span> &nbsp;
        <span style="color:#f85149">&#9660; {n_down} a la baja</span> &nbsp;&middot;&nbsp;
        Ganador: <b style="color:#3fb950">{winner}</b> &nbsp;
        Perdedor: <b style="color:#f85149">{loser}</b>
      </div>
    </div>
  </div>
  <div class="radar-scroll">
    <table>
      <thead><tr>
        <th>Activo</th><th>Movimiento</th><th>Noticia del dia</th><th>Accion del sistema</th>
      </tr></thead>
      <tbody>{radar_rows}</tbody>
    </table>
  </div>
</div>"""

    macro  = signals_data.get("macro",{})
    reason = macro.get("regime_reason","")
    regime_note = f'<p class="ts" style="margin-bottom:16px">{_esc(reason)}</p>' if reason else ""

    disc_html = _discovery_section(discovery_data)

    return f"""
<div class="tab-content" id="tab-mercado">
  {regime_note}
  <div class="two-col">
    {sector_html}
    {radar_html}
  </div>
  <div class="section-gap"></div>
  {disc_html}
</div>"""


# ─── CSS ──────────────────────────────────────────────────────────────────────

_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}

:root{
  --bg:#0d1117; --s1:#161b22; --s2:#21262d; --bd:#30363d;
  --tx:#e6edf3; --mt:#7d8590; --ac:#58a6ff;
}

html{scroll-behavior:smooth}
body{font-family:'Inter',system-ui,sans-serif;background:var(--bg);color:var(--tx);
  padding:0 0 60px;max-width:1480px;margin:0 auto;line-height:1.5}

/* ── freshness banner ── */
.fresh-banner{
  padding:10px 28px;font-size:.82rem;font-weight:600;margin-bottom:12px;
  border-radius:0;letter-spacing:.2px;
}
.fresh-warn{background:#854d0e22;color:#d29922;border-left:3px solid #d29922}
.fresh-stale{background:#7f1d1d33;color:#f85149;border-left:3px solid #f85149}

/* ── progress bar ── */
.rbar{height:2px;background:linear-gradient(90deg,#58a6ff,#bc8cff);
  position:fixed;top:0;left:0;width:0%;transition:width 300s linear;
  border-radius:0 2px 2px 0;z-index:100}

/* ── top bar ── */
.topbar{
  display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;
  padding:16px 28px 0;margin-bottom:20px;
}
.logo{font-size:1.3rem;font-weight:800;
  background:linear-gradient(135deg,#58a6ff,#bc8cff);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.topbar-meta{font-size:.78rem;color:var(--mt);display:flex;align-items:center;gap:10px}
.live{display:inline-block;width:7px;height:7px;background:#3fb950;
  border-radius:50%;animation:blink 2s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}

/* ── nav tabs ── */
.nav{
  display:flex;gap:4px;padding:0 28px;
  border-bottom:1px solid var(--bd);margin-bottom:28px;
  overflow-x:auto;-webkit-overflow-scrolling:touch;
}
.tab-btn{
  padding:10px 22px;font-size:.85rem;font-weight:600;color:var(--mt);
  background:none;border:none;border-bottom:2px solid transparent;
  cursor:pointer;white-space:nowrap;transition:color .15s,border-color .15s;
  margin-bottom:-1px;
}
.tab-btn:hover{color:var(--tx)}
.tab-btn.active{color:var(--ac);border-bottom-color:var(--ac)}

/* ── tab content ── */
.tab-content{display:none;padding:0 28px;animation:fadeIn .2s ease}
.tab-content.active{display:block}
@keyframes fadeIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}

/* ── KPI rows ── */
.kpi-row{
  display:grid;
  grid-template-columns:repeat(auto-fill,minmax(180px,1fr));
  gap:12px;margin-bottom:4px;
}
.kpi-row-adv{
  grid-template-columns:repeat(auto-fill,minmax(200px,1fr));
  margin-top:12px;margin-bottom:0;
}
.kpi{
  background:var(--s1);border:1px solid var(--bd);border-radius:10px;
  padding:16px 18px;transition:transform .15s,box-shadow .15s;
}
.kpi:hover{transform:translateY(-2px);box-shadow:0 6px 20px rgba(0,0,0,.4)}
.kpi-hero{
  background:linear-gradient(135deg,#161b22,#1c2331);
  border-color:#58a6ff33;
}
.kpi-lbl{font-size:.68rem;color:var(--mt);text-transform:uppercase;
  letter-spacing:.7px;margin-bottom:6px;font-weight:600}
.kpi-val{font-size:1.45rem;font-weight:700;line-height:1.1;margin-bottom:4px}
.kpi-hero-val{font-size:1.75rem}
.kpi-sub{font-size:.73rem;color:var(--mt)}
.kpi-tag{font-size:.75rem;font-weight:600;padding:3px 10px;border-radius:20px;
  display:inline-block;margin-top:6px}

/* ── cards ── */
.card{background:var(--s1);border:1px solid var(--bd);border-radius:12px;
  padding:20px 22px;margin-bottom:4px;transition:box-shadow .15s}
.card:hover{box-shadow:0 4px 16px rgba(0,0,0,.3)}
.card-head{display:flex;align-items:flex-start;justify-content:space-between;
  flex-wrap:wrap;gap:10px;margin-bottom:18px}
.card-title{font-size:.95rem;font-weight:700;color:var(--tx);margin-bottom:3px}
.card-sub{font-size:.78rem;color:var(--mt)}
.pill{font-size:.82rem;font-weight:700;background:var(--s2);
  border:1px solid var(--bd);border-radius:20px;padding:4px 12px;white-space:nowrap}

/* ── two-col layout ── */
.two-col{display:grid;grid-template-columns:1fr 2fr;gap:16px;align-items:start}
@media(max-width:900px){.two-col{grid-template-columns:1fr}}

/* ── section gap ── */
.section-gap{height:16px}

/* ── positions ── */
.pos-summary{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));
  gap:12px;margin-bottom:20px}
.pos-layout{display:flex;gap:24px;flex-wrap:wrap;align-items:flex-start}
.pos-chart-col{flex:0 0 220px;display:flex;flex-direction:column;align-items:center}
.pos-cards-col{flex:1;min-width:280px;display:grid;
  grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px}
.pos-card{background:var(--s2);border:1px solid var(--bd);border-radius:10px;
  padding:14px 16px;transition:background .15s,box-shadow .15s}
.pos-card:hover{background:#1c2331;box-shadow:0 4px 12px rgba(0,0,0,.4)}
.pos-top{display:flex;justify-content:space-between;margin-bottom:10px}
.pos-ticker{font-size:1.05rem;font-weight:800}
.pos-sub{font-size:.72rem;color:var(--mt);margin-top:2px}
.pos-pnl{font-size:.9rem;font-weight:700;text-align:right}
.pos-pnl-pct{font-size:.78rem;text-align:right;margin-top:2px}
.pos-bar-track{height:4px;background:var(--bd);border-radius:2px;margin-bottom:10px}
.pos-meta{display:flex;gap:14px;flex-wrap:wrap;font-size:.75rem;color:var(--mt)}
.pos-meta b{color:var(--tx)}

/* ── alloc legend ── */
.alloc-legend{margin-top:14px;width:100%}
.leg-row{display:flex;align-items:center;gap:7px;font-size:.77rem;
  color:var(--tx);margin-bottom:5px}
.leg-dot{width:8px;height:8px;border-radius:2px;flex-shrink:0}

/* ── signals ── */
.sleeve-block{margin-bottom:28px}
.sleeve-header{display:flex;align-items:center;gap:12px;
  padding:10px 14px;background:var(--s2);border-radius:8px;margin-bottom:14px}
.sleeve-label{font-size:.88rem;font-weight:700}
.sleeve-count{font-size:.78rem}
.sig-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px}
.sig-card{background:var(--s2);border:1px solid var(--bd);border-radius:10px;
  padding:14px 16px;cursor:pointer;transition:background .15s,box-shadow .15s}
.sig-card:hover{background:#1c2331;box-shadow:0 4px 12px rgba(0,0,0,.3)}
.sig-head{display:flex;justify-content:space-between;align-items:center;
  flex-wrap:wrap;gap:8px;margin-bottom:10px}
.sig-left{display:flex;align-items:center;gap:8px}
.sig-right{display:flex;align-items:center;gap:10px}
.sig-ticker{font-size:1.05rem;font-weight:800}
.conv-badge{font-size:.7rem;font-weight:700;padding:2px 8px;border-radius:20px}
.sig-alloc{font-size:.82rem;font-weight:600;color:var(--tx)}
.sig-arrow{font-size:.72rem;color:var(--mt);transition:transform .2s}
.sig-arrow.open{transform:rotate(180deg)}
.sig-pills{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
.spill{background:var(--s1);border:1px solid var(--bd);border-radius:7px;
  padding:5px 10px;min-width:80px;text-align:center}
.spill-l{font-size:.65rem;color:var(--mt);margin-bottom:2px}
.spill-v{font-size:.82rem;font-weight:700}
.conv-track{height:3px;background:var(--bd);border-radius:2px}
.conv-fill{height:3px;border-radius:2px;opacity:.75}
.sig-thesis{display:none;margin-top:12px;padding:12px 14px;
  background:var(--s1);border-radius:8px;border-left:3px solid var(--ac);
  font-size:.82rem;color:#94a3b8;line-height:1.65;animation:fadeIn .2s ease}
.ws-block{margin-top:12px;padding:10px 12px;background:#0d1117;border-radius:7px;
  border:1px solid #21262d;font-size:.8rem}
.ws-header{display:flex;align-items:center;gap:4px;margin-bottom:6px;flex-wrap:wrap}
.ws-label{font-size:.65rem;font-weight:700;letter-spacing:.8px;color:#8b949e;
  background:#161b22;padding:2px 6px;border-radius:4px}
.ws-rec{font-weight:700;padding:2px 8px;border-radius:4px;border:1px solid;font-size:.8rem}
.ws-pt{font-weight:700;font-size:.82rem}
.ws-val{font-size:.75rem}
.ws-thesis{color:#94a3b8;margin:4px 0 8px;line-height:1.5}
.ws-lists{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.ws-col-h{font-size:.68rem;font-weight:700;letter-spacing:.5px;color:#58a6ff;
  text-transform:uppercase;margin-bottom:4px}
.ws-col ul{margin:0;padding-left:14px;color:#8b949e;line-height:1.6}

/* ── discovery ── */
.disc-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px;margin-top:8px}
.disc-card{background:var(--s1);border:1px solid var(--bd);border-radius:10px;padding:14px}
.disc-head{display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap}
.disc-ticker{font-weight:800;font-size:1rem;color:#e6edf3}
.disc-prior{font-size:.68rem;font-weight:700;padding:2px 7px;border-radius:12px;border:1px solid;letter-spacing:.4px}
.disc-rep{font-size:.65rem;background:#d2922222;color:#d29922;border:1px solid #d2922255;
  padding:2px 7px;border-radius:12px;font-weight:600}
.disc-razon{font-size:.8rem;color:#94a3b8;margin:0 0 6px;line-height:1.55}
.disc-riesgo{font-size:.75rem;color:#7d8590;margin:0}

/* ── calendar ── */
.cal-wrap{overflow-x:auto}
.cal-table{border-collapse:separate;border-spacing:4px;width:100%;table-layout:fixed}
.cal-th{color:var(--mt);font-size:.7rem;font-weight:600;text-transform:uppercase;
  letter-spacing:.5px;padding:5px 4px;text-align:center}
.cal-cell{border-radius:8px;padding:8px 6px 6px;text-align:center;vertical-align:top;
  min-width:68px;height:74px;background:var(--s2);cursor:default;
  transition:transform .12s,box-shadow .12s}
.cal-cell:hover{transform:scale(1.06);box-shadow:0 4px 16px rgba(0,0,0,.5);
  z-index:2;position:relative}
.cal-empty{background:none}
.cal-wk{opacity:.35}
.cal-nd{opacity:.5}
.cal-n{display:block;font-size:.65rem;color:var(--mt);font-weight:600;margin-bottom:5px}
.cal-p{display:block;font-size:.8rem;font-weight:700;line-height:1.2}
.cal-q{display:block;font-size:.66rem;margin-top:2px;opacity:.85}

/* ── table ── */
table{width:100%;border-collapse:collapse;font-size:.82rem}
th{color:var(--mt);text-align:left;padding:8px 10px;border-bottom:1px solid var(--bd);
  font-weight:600;font-size:.72rem;text-transform:uppercase;letter-spacing:.5px}
td{padding:9px 10px;border-bottom:1px solid var(--bd);vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:var(--s2)}
.rt-ticker{font-weight:700}
.radar-scroll{overflow-y:auto;max-height:520px}

/* ── misc ── */
.ts{font-size:.78rem;color:var(--mt)}
.muted{color:var(--mt)}

@media(max-width:640px){
  .topbar,.nav,.tab-content{padding-left:14px;padding-right:14px}
  .kpi-val{font-size:1.15rem}
  .kpi-hero-val{font-size:1.4rem}
  .sig-grid{grid-template-columns:1fr}
  .pos-chart-col{flex:0 0 100%}
}
"""


# ─── HTML completo ─────────────────────────────────────────────────────────────

def build_html(equity, initial, history, positions, signals_data,
               spy_history=None, qqq_history=None, metrics=None, perf_data=None, discovery_data=None):
    if spy_history is None:
        spy_history = []
    if qqq_history is None:
        qqq_history = []
    if metrics is None:
        metrics = {}

    macro  = signals_data.get("macro", {})
    regime = macro.get("regime","unknown")
    pr     = macro.get("prices", {}) or {}
    vix    = pr.get("vix",0) or 0
    wti    = pr.get("oil_wti",0) or 0
    gold   = pr.get("gold",0) or 0
    dxy    = pr.get("dxy",0) or 0
    now    = datetime.now().strftime("%d de %B de %Y — %H:%M")

    # Freshness
    age_hours = 99.0
    gen_at_str = signals_data.get("generated_at","")
    if gen_at_str:
        try:
            gen_dt = datetime.fromisoformat(gen_at_str.replace("Z","+00:00"))
            if gen_dt.tzinfo is None:
                gen_dt = gen_dt.replace(tzinfo=timezone.utc)
            age_hours = (datetime.now(timezone.utc) - gen_dt).total_seconds() / 3600
        except Exception:
            pass

    t_resumen    = _tab_resumen(equity, initial, regime, vix, wti, gold, dxy,
                                history, spy_history, signals_data, metrics, age_hours,
                                perf_data=perf_data, qqq_history=qqq_history)
    t_posiciones = _tab_posiciones(positions)
    t_senales    = _tab_senales(signals_data)
    t_mercado    = _tab_mercado(signals_data, discovery_data=discovery_data)

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="refresh" content="300">
  <title>Alpha Dashboard</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
  <style>{_CSS}</style>
</head>
<body>

<div class="rbar" id="rbar"></div>

<div class="topbar">
  <div class="logo">Alpha Dashboard</div>
  <div class="topbar-meta">
    <span><span class="live"></span> {now}</span>
    <span>&middot; Paper Trading</span>
    <span id="countdown" style="color:var(--mt);font-size:.78rem"></span>
  </div>
</div>

<nav class="nav">
  <button class="tab-btn active" data-tab="resumen">Resumen</button>
  <button class="tab-btn" data-tab="posiciones">Posiciones</button>
  <button class="tab-btn" data-tab="senales">Senales</button>
  <button class="tab-btn" data-tab="mercado">Mercado</button>
</nav>

{t_resumen}
{t_posiciones}
{t_senales}
{t_mercado}

<script>
// ── tabs ──
const tabs = document.querySelectorAll('.tab-btn');
tabs.forEach(btn => {{
  btn.addEventListener('click', () => {{
    tabs.forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
    setTimeout(() => {{
      if (window._charts) Object.values(window._charts).forEach(ch => ch && ch.resize && ch.resize());
    }}, 50);
  }});
}});

// ── thesis toggle ──
function toggleSig(id) {{
  const el  = document.getElementById(id);
  const arr = document.getElementById('arrow_' + id);
  const open = el.style.display === 'block';
  el.style.display = open ? 'none' : 'block';
  arr.classList.toggle('open', !open);
}}

// ── progress bar + countdown ──
setTimeout(() => document.getElementById('rbar').style.width = '100%', 100);
(function() {{
  let s = 300;
  const el = document.getElementById('countdown');
  const iv = setInterval(() => {{
    s--;
    if (s <= 0) {{ clearInterval(iv); return; }}
    const m = Math.floor(s/60), sc = s % 60;
    el.textContent = '· Actualiza en ' + m + ':' + sc.toString().padStart(2,'0');
  }}, 1000);
}})();
</script>

</body>
</html>"""


# ─── generate + main ──────────────────────────────────────────────────────────

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

    # Portfolio history (1M)
    history: list[dict] = []
    try:
        from alpaca.trading.requests import GetPortfolioHistoryRequest
        req = GetPortfolioHistoryRequest(period="1M", timeframe="1D")
        ph  = broker._trading.get_portfolio_history(req)
        if ph and ph.equity:
            history = [
                {"ts": int(t), "equity": float(e)}
                for t, e in zip(ph.timestamp or [], ph.equity)
                if e is not None and float(e) > 0
            ]
        logger.info("Portfolio history: %d entradas validas", len(history))
    except Exception as e:
        logger.warning("Portfolio history no disponible: %s", e)

    # SPY + QQQ benchmarks (con cache de 1h para no re-descargar en cada refresh)
    spy_history: list[dict] = []
    qqq_history: list[dict] = []
    bench_cache_path = BASE_DIR / "signals" / "benchmarks_cache.json"

    def _load_bench_cache() -> dict:
        if bench_cache_path.exists():
            try:
                data = json.loads(bench_cache_path.read_text(encoding="utf-8"))
                age_s = (datetime.now().timestamp() - data.get("ts", 0))
                if age_s < 3600:  # cache válido por 1h
                    return data
            except Exception:
                pass
        return {}

    def _save_bench_cache(spy: list, qqq: list) -> None:
        try:
            bench_cache_path.write_text(
                json.dumps({"ts": datetime.now().timestamp(), "spy": spy, "qqq": qqq}),
                encoding="utf-8",
            )
        except Exception:
            pass

    if len(history) >= 2:
        cached = _load_bench_cache()
        if cached.get("spy") and cached.get("qqq"):
            spy_history = cached["spy"]
            qqq_history = cached["qqq"]
            logger.info("Benchmarks cargados desde cache (%d SPY, %d QQQ)", len(spy_history), len(qqq_history))
        else:
            try:
                import yfinance as yf
                bench_df = yf.download("SPY QQQ", period="1mo", progress=False, auto_adjust=True)
                if not bench_df.empty:
                    close = bench_df["Close"]
                    if hasattr(close, "squeeze") and close.ndim == 1:
                        close = close.to_frame()
                    for ticker, hist_list in [("SPY", spy_history), ("QQQ", qqq_history)]:
                        if ticker in close.columns:
                            col = close[ticker].dropna()
                            if len(col) >= 2:
                                base = float(col.iloc[0])
                                for ts, v in zip(col.index, col.values):
                                    hist_list.append({"ts": int(ts.timestamp()), "equity": float(v) / base})
                    # Normalizar las listas (equity = ratio, no $ absolutos)
                    logger.info("Benchmarks descargados: %d SPY, %d QQQ", len(spy_history), len(qqq_history))
                    _save_bench_cache(spy_history, qqq_history)
            except Exception as e:
                logger.warning("Benchmarks no disponibles: %s", e)

    # Metrics
    metrics = _calc_metrics(history, spy_history, qqq_history)
    if metrics:
        logger.info("Metricas: Sortino=%.2f MaxDD=%.2f%% Alpha1M=%s",
                    metrics.get("sortino",0), metrics.get("max_dd",0),
                    metrics.get("alpha_1m","N/A"))

    # Signals
    signals_data: dict = {}
    if SIGNALS_PATH.exists():
        try:
            signals_data = json.loads(SIGNALS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Fix radar prices: re-download live 1D move for top radar tickers
    radar_entries = (signals_data.get("radar") or {}).get("entries", [])
    if radar_entries:
        try:
            import yfinance as yf
            radar_tickers = [e.get("ticker", "") for e in radar_entries if e.get("ticker")]
            if radar_tickers:
                prices_df = yf.download(" ".join(radar_tickers), period="2d", progress=False, auto_adjust=True)
                close = prices_df.get("Close") if not prices_df.empty else None
                if close is not None:
                    if hasattr(close, "squeeze") and close.ndim == 1:
                        close = close.to_frame(name=radar_tickers[0])
                    for entry in radar_entries:
                        t = entry.get("ticker", "")
                        if t in close.columns:
                            col = close[t].dropna()
                            if len(col) >= 2:
                                prev_close = float(col.iloc[-2])
                                last_close = float(col.iloc[-1])
                                entry["pct_1d"] = (last_close - prev_close) / prev_close if prev_close else 0
                                entry["move_pct"] = entry["pct_1d"]
                logger.info("Radar: precios live actualizados para %d tickers", len(radar_tickers))
        except Exception as e:
            logger.debug("Radar live prices no disponibles: %s", e)

    # Performance log (semanal vs SPY)
    perf_data: dict | None = None
    perf_path = BASE_DIR / "signals" / "performance_log.json"
    if perf_path.exists():
        try:
            perf_data = json.loads(perf_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Discovery (candidatos fuera del universo)
    discovery_data: dict | None = None
    disc_path = BASE_DIR / "signals" / "discovery.json"
    if disc_path.exists():
        try:
            discovery_data = json.loads(disc_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    initial = signals_data.get("capital_usd", PARAMS.paper_capital_usd)
    html    = build_html(equity, initial, history, positions, signals_data,
                         spy_history=spy_history, qqq_history=qqq_history,
                         metrics=metrics, perf_data=perf_data, discovery_data=discovery_data)
    OUT_PATH.write_text(html, encoding="utf-8")
    logger.info("Dashboard generado -> %s (%d bytes)", OUT_PATH, len(html))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--watch",   action="store_true")
    args = parser.parse_args()

    load_dotenv(BASE_DIR / ".env")
    try:
        generate()
    except Exception as exc:
        import traceback
        logger.error("Error generando dashboard:\n%s", traceback.format_exc())
        DOCS_DIR.mkdir(exist_ok=True)
        OUT_PATH.write_text(
            f"<html><body style='background:#0d1117;color:#f85149;font-family:monospace;padding:40px'>"
            f"<h2>Dashboard no disponible</h2><pre>{exc}</pre></body></html>",
            encoding="utf-8",
        )

    if not args.no_open:
        import webbrowser
        webbrowser.open(OUT_PATH.as_uri())

    if args.watch:
        while True:
            time.sleep(300)
            try:
                generate()
            except Exception:
                pass


if __name__ == "__main__":
    main()
