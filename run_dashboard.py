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


# ─── WORKFLOW HEALTH PANEL ────────────────────────────────────────────────────

def _advanced_metrics_panel(history, qqq_history, spy_history, brk_history=None) -> str:
    """Iter11/12: métricas risk-adjusted vs QQQ + carrera vs SPY/QQQ/Buffett.

    El retorno absoluto miente en un tech bull (QQQ siempre gana). Estas
    métricas miden si el portfolio es bueno AJUSTADO POR RIESGO:
    - Carrera de retornos: Portfolio vs SPY vs QQQ vs BRK-B (Buffett) misma ventana
    - Calmar: retorno anualizado / max drawdown (eficiencia del riesgo)
    - Beta vs QQQ: cuánto se mueve con el Nasdaq (1.0 = igual, <1 defensivo)
    - Up/Down capture: % de las subidas/bajadas de QQQ que captura
    - Information Ratio: alpha vs QQQ / tracking error (consistencia del alpha)
    - Batting average: % de días que el portfolio supera al QQQ
    """
    brk_history = brk_history or []

    def _rets(hist):
        vals = [h.get("equity", h.get("v")) for h in hist if h.get("equity", h.get("v")) is not None]
        return [(vals[i] - vals[i-1]) / vals[i-1] for i in range(1, len(vals)) if vals[i-1] > 0]

    def _total_ret(hist):
        vals = [h.get("equity", h.get("v")) for h in hist if h.get("equity", h.get("v")) is not None]
        return ((vals[-1] - vals[0]) / vals[0] * 100) if len(vals) >= 2 and vals[0] > 0 else None

    # ── Carrera de retornos (misma ventana de días que tenga el portfolio) ──
    n_days = len([h for h in (history or []) if h.get("equity", h.get("v")) is not None])
    race_rows = ""
    if n_days >= 2:
        contenders = [
            ("Portfolio", _total_ret(history), "#3fb950"),
            ("S&P 500 (SPY)", _total_ret((spy_history or [])[-n_days:]), "#58a6ff"),
            ("Nasdaq (QQQ)", _total_ret((qqq_history or [])[-n_days:]), "#bc8cff"),
            ("Buffett (BRK-B)", _total_ret((brk_history or [])[-n_days:]), "#d29922"),
        ]
        valid = [(nm, r, c) for nm, r, c in contenders if r is not None]
        best = max((r for _, r, _ in valid), default=0)
        bars = []
        span = max(abs(r) for _, r, _ in valid) or 1.0
        for nm, r, c in sorted(valid, key=lambda x: -x[1]):
            width = min(100, abs(r) / span * 100)
            crown = " 👑" if r == best else ""
            bars.append(f"""
        <div style="display:flex;align-items:center;gap:8px;margin:5px 0">
          <span style="width:120px;font-size:.72rem;color:var(--mt)">{nm}{crown}</span>
          <div style="flex:1;background:var(--s2);border-radius:3px;height:18px;position:relative">
            <div style="width:{width:.0f}%;background:{c};height:100%;border-radius:3px;
                        min-width:2px;opacity:.85"></div>
          </div>
          <span style="width:64px;text-align:right;font-family:var(--mono);font-size:.78rem;
                       font-weight:600;color:{c}">{r:+.2f}%</span>
        </div>""")
        race_rows = f"""<div style="margin-bottom:14px">
      <div style="font-size:.68rem;color:var(--mt);text-transform:uppercase;letter-spacing:.6px;margin-bottom:6px">
        Carrera de retornos · últimos {n_days} snapshots
      </div>
      {''.join(bars)}
    </div>"""

    port_r = _rets(history or [])
    qqq_r = _rets(qqq_history or [])
    n = min(len(port_r), len(qqq_r))

    if n < 3:
        # Sin data suficiente para risk-adjusted, pero mostramos la carrera si la hay
        if race_rows:
            return f"""<div class="card">
  <div class="card-head"><div>
    <div class="card-title">PORTFOLIO vs BENCHMARKS</div>
    <div class="card-sub">Pocos datos aún para métricas risk-adjusted</div>
  </div></div>
  {race_rows}
</div>"""
        return ""  # no hay data suficiente

    port_r = port_r[-n:]
    qqq_r = qqq_r[-n:]

    # Calmar: retorno anualizado / max drawdown del portfolio
    vals = [h.get("equity", h.get("v")) for h in history if h.get("equity", h.get("v")) is not None]
    total_ret = (vals[-1] - vals[0]) / vals[0] if vals and vals[0] > 0 else 0
    arr = ((1 + total_ret) ** (252 / max(len(vals), 1)) - 1) if total_ret > -1 else -1
    peak, max_dd = vals[0], 0.0
    for v in vals:
        peak = max(peak, v)
        max_dd = max(max_dd, (peak - v) / peak if peak > 0 else 0)
    calmar = (arr / max_dd) if max_dd > 0.001 else 0

    # Beta vs QQQ
    mean_p = sum(port_r) / n
    mean_q = sum(qqq_r) / n
    cov = sum((port_r[i] - mean_p) * (qqq_r[i] - mean_q) for i in range(n)) / n
    var_q = sum((q - mean_q) ** 2 for q in qqq_r) / n
    beta = (cov / var_q) if var_q > 0 else 0

    # Up / Down capture vs QQQ
    up_p = [port_r[i] for i in range(n) if qqq_r[i] > 0]
    up_q = [qqq_r[i] for i in range(n) if qqq_r[i] > 0]
    dn_p = [port_r[i] for i in range(n) if qqq_r[i] < 0]
    dn_q = [qqq_r[i] for i in range(n) if qqq_r[i] < 0]
    up_cap = (sum(up_p) / sum(up_q) * 100) if up_q and sum(up_q) != 0 else 0
    dn_cap = (sum(dn_p) / sum(dn_q) * 100) if dn_q and sum(dn_q) != 0 else 0

    # Information Ratio: alpha vs QQQ / tracking error
    active = [port_r[i] - qqq_r[i] for i in range(n)]
    mean_active = sum(active) / n
    te = (sum((a - mean_active) ** 2 for a in active) / n) ** 0.5
    info_ratio = (mean_active / te * (252 ** 0.5)) if te > 0 else 0

    # Batting average: % de días que port > qqq
    batting = sum(1 for i in range(n) if port_r[i] > qqq_r[i]) / n * 100

    def _cell(label, value, good, sub):
        color = "#3fb950" if good else ("#d29922" if good is None else "#f85149")
        return f"""<div style="padding:12px;background:var(--s2);border-radius:4px">
          <div style="font-size:.62rem;color:var(--mt);text-transform:uppercase;letter-spacing:.6px">{label}</div>
          <div style="font-family:var(--mono);font-size:1.3rem;font-weight:600;color:{color}">{value}</div>
          <div style="font-size:.64rem;color:var(--mt)">{sub}</div>
        </div>"""

    cells = "".join([
        _cell("Calmar Ratio", f"{calmar:.2f}", calmar > 1.0, "ret anual / max DD · >1 bueno"),
        _cell("Beta vs QQQ", f"{beta:.2f}", None, "1.0=igual Nasdaq · <1 defensivo"),
        _cell("Up Capture", f"{up_cap:.0f}%", up_cap > 80, "de las subidas de QQQ"),
        _cell("Down Capture", f"{dn_cap:.0f}%", dn_cap < 80, "de las bajadas (menor=mejor)"),
        _cell("Information Ratio", f"{info_ratio:.2f}", info_ratio > 0, "alpha/TE vs QQQ · >0.5 bueno"),
        _cell("Batting Avg", f"{batting:.0f}%", batting > 50, "% dias que supera QQQ"),
    ])

    # Veredicto honesto
    if up_cap > 100 and dn_cap < 100:
        verdict = "🟢 Capturas mas subidas que bajadas vs QQQ — perfil ideal"
    elif dn_cap < up_cap:
        verdict = "🟡 Down capture < up capture — defendes bien aunque captures menos"
    else:
        verdict = "🔴 Capturas mas bajadas que subidas vs QQQ — revisar seleccion"

    return f"""<div class="card">
  <div class="card-head">
    <div>
      <div class="card-title">PORTFOLIO vs BENCHMARKS · RISK-ADJUSTED</div>
      <div class="card-sub">El retorno absoluto miente en bull. Esto mide eficiencia del riesgo.</div>
    </div>
  </div>
  {race_rows}
  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-top:8px">
    {cells}
  </div>
  <div style="margin-top:12px;padding-top:8px;border-top:1px solid var(--bd);font-size:.72rem;color:var(--mt)">
    {verdict}
  </div>
</div>"""


def _deployment_panel(equity, positions, signals_data) -> str:
    """Iter15: medidor de despliegue/exposición. Invertido vs target vs cash.

    Cazaría al instante un caso como el de hoy (44% desplegado cuando el target
    era ~98%). Lee el target del allocation (signals.params: CP + OPT).
    """
    if not equity or equity <= 0:
        return ""
    invested = sum((p.market_value or 0) for p in (positions or []) if (p.market_value or 0) > 1.0)
    dust = sum((p.market_value or 0) for p in (positions or []) if 0 < (p.market_value or 0) <= 1.0)
    cash = max(0.0, equity - invested - dust)
    dep_pct = invested / equity * 100
    cash_pct = cash / equity * 100

    params = (signals_data or {}).get("params", {}) or {}
    target_pct = (float(params.get("weight_short_term", 0) or 0)
                  + float(params.get("weight_options", 0) or 0)) * 100
    if target_pct <= 0:
        target_pct = 90.0  # fallback razonable (perfil agresivo)
    gap = target_pct - dep_pct

    if gap <= 8:
        color, verdict = "#3fb950", f"🟢 Desplegado según target ({dep_pct:.0f}% vs {target_pct:.0f}%)"
    elif gap <= 25:
        color, verdict = "#d29922", f"🟡 Sub-desplegado: {gap:.0f}pp por debajo del target ({target_pct:.0f}%)"
    else:
        color, verdict = "#f85149", f"🔴 MUY sub-desplegado: {gap:.0f}pp de capital ocioso vs target {target_pct:.0f}%"

    # barra apilada: invertido (color) + cash (gris) + marcador de target
    return f"""<div class="card">
  <div class="card-head"><div>
    <div class="card-title">DESPLIEGUE DE CAPITAL</div>
    <div class="card-sub">Cuánto capital está realmente trabajando vs el target del allocation</div>
  </div>
  <div style="text-align:right">
    <div style="font-family:var(--mono);font-size:1.6rem;font-weight:600;color:{color}">{dep_pct:.0f}%</div>
    <div style="font-family:var(--mono);font-size:.78rem;color:var(--mt)">invertido</div>
  </div></div>
  <div style="position:relative;height:26px;background:#30363d;border-radius:4px;overflow:hidden;margin-top:6px">
    <div style="width:{min(dep_pct,100):.0f}%;height:100%;background:{color};opacity:.85"></div>
    <div style="position:absolute;top:0;left:{min(target_pct,100):.0f}%;height:100%;width:2px;background:#fff"></div>
    <div style="position:absolute;top:3px;left:8px;font-size:.7rem;color:#fff;font-family:var(--mono)">
      ${invested:,.0f} invertido · ${cash:,.0f} cash ({cash_pct:.0f}%)
    </div>
  </div>
  <div style="display:flex;justify-content:space-between;font-size:.64rem;color:var(--mt);margin-top:3px">
    <span>0%</span><span>↑ target {target_pct:.0f}%</span><span>100%</span>
  </div>
  <div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--bd);font-size:.74rem;color:{color}">
    {verdict}
  </div>
</div>"""


def _learnings_panel() -> str:
    """Iter13: "Segundo Cerebro" — lecciones que el sistema aprendió de sus trades.

    Lee trade_db.summarize_learnings(): tickers con historial fuerte (favorable o
    adverso) que el scorer usa para subir/bajar score. Es la memoria machine-readable
    que reemplaza la idea de Obsidian — vive en la SQLite y alimenta decisiones.
    """
    try:
        from alpha_agent.analytics.trade_db import summarize_learnings
        lessons = summarize_learnings(limit=8)
    except Exception as e:
        return ""
    if not lessons:
        return ""
    rows = []
    for l in lessons:
        is_adv = "ADVERSO" in l
        color = "#f85149" if is_adv else "#3fb950"
        icon = "🔴" if is_adv else "🟢"
        rows.append(f"""<div style="display:flex;gap:8px;padding:6px 10px;font-size:.74rem;
                    border-left:3px solid {color};background:{color}11;margin:4px 0">
          <span>{icon}</span><span style="color:var(--mt)">{l}</span></div>""")
    return f"""<div class="card">
  <div class="card-head"><div>
    <div class="card-title">🧠 SEGUNDO CEREBRO · memoria de trades</div>
    <div class="card-sub">El sistema aprende: sube los que históricamente gana, baja/evita los que pierde</div>
  </div></div>
  {''.join(rows)}
  <div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--bd);font-size:.7rem;color:var(--mt)">
    favorable → score +0.25 · adverso → score -0.50 (se aplica al scorer CP cada corrida)
  </div>
</div>"""


def _control_center_panel() -> str:
    """Iter11: card "Control Center" con botones que ejecutan via Flask fetch().

    Antes (iter10) usaba links t.me/bot?text= pero Telegram BLOQUEA pre-llenar
    mensajes a bots. Ahora los botones llaman a /api/cmd/<action> del Flask
    local (mismo que sirve el bot). Funciona desde http://localhost:5050/dashboard
    o desde file:/// (con CORS). Resultado se muestra en un panel inline.
    """
    import os as _os
    _is_paused = (BASE_DIR / "signals" / "paused.flag").exists()
    _anthropic_on = _os.getenv("ENABLE_ANTHROPIC", "").lower() in ("true", "1", "yes")

    pause_status = "⏸️ PAUSADO" if _is_paused else "▶️ ACTIVO"
    pause_color = "#f85149" if _is_paused else "#3fb950"
    pause_action = "resume" if _is_paused else "pause"
    pause_label = "▶️ Reanudar trading" if _is_paused else "⏸️ Pausar trading"

    anthropic_status = "🟢 ON" if _anthropic_on else "🔴 OFF"
    anthropic_color = "#3fb950" if _anthropic_on else "#6e7681"
    anthropic_action = "anthropic_off" if _anthropic_on else "anthropic_on"
    anthropic_label = "🔴 Apagar Anthropic" if _anthropic_on else "🟢 Activar Anthropic"

    _btn = ("margin:6px 6px 0 0;padding:8px 14px;border-radius:4px;"
            "font-size:.74rem;font-family:var(--mono);border:1px solid;"
            "cursor:pointer;background:transparent")

    # JS helper: detecta origin (file:// vs http) y hace fetch al Flask.
    js = """<script>
(function(){
  if (window.__alphaCmdDefined) return;
  window.__alphaCmdDefined = true;
  const API = (location.protocol === 'file:') ? 'http://localhost:5050' : '';
  window.alphaCmd = async function(action, confirmMsg){
    if (confirmMsg && !confirm(confirmMsg)) return;
    const out = document.getElementById('cc-result');
    if (out){ out.style.display='block'; out.textContent='Ejecutando '+action+'...'; }
    try {
      const r = await fetch(API + '/api/cmd/' + action, {method:'POST'});
      const d = await r.json();
      if (out){ out.textContent = d.result || d.error || 'OK'; }
    } catch(e) {
      if (out){
        out.textContent = 'No se pudo conectar al dashboard Flask.\\n' +
          'Abri http://localhost:5050/dashboard en vez de file://, o asegurate ' +
          'que start_dashboard.ps1 este corriendo.\\nError: ' + e;
      }
    }
  };
})();
</script>"""

    return f"""<div class="card">
  {js}
  <div class="card-head">
    <div>
      <div class="card-title">🎮 CONTROL CENTER</div>
      <div class="card-sub">Botones ejecutan directo via Flask local (sin Telegram)</div>
    </div>
    <div style="text-align:right;display:flex;flex-direction:column;gap:4px">
      <div style="font-family:var(--mono);font-size:.78rem">
        Trading: <span style="color:{pause_color};font-weight:600">{pause_status}</span>
      </div>
      <div style="font-family:var(--mono);font-size:.78rem">
        Anthropic (local): <span style="color:{anthropic_color};font-weight:600">{anthropic_status}</span>
      </div>
    </div>
  </div>

  <div style="margin-top:10px">
    <div style="font-size:.66rem;color:var(--mt);margin-bottom:6px;text-transform:uppercase;letter-spacing:.5px">Trading control</div>
    <button onclick="alphaCmd('{pause_action}')" style="{_btn};color:{pause_color};border-color:{pause_color}">{pause_label}</button>
    <button onclick="alphaCmd('force_daily','Forzar un alpha-daily extra ahora?')" style="{_btn};color:#3b82f6;border-color:#3b82f6">🚀 Forzar daily ahora</button>
    <button onclick="alphaCmd('liquidate_orphans','Liquidar TODAS las huerfanas con P&L negativo?')" style="{_btn};color:#f85149;border-color:#f85149">🔴 Liquidar huerfanas negativas</button>
  </div>

  <div style="margin-top:14px">
    <div style="font-size:.66rem;color:var(--mt);margin-bottom:6px;text-transform:uppercase;letter-spacing:.5px">LLM</div>
    <button onclick="alphaCmd('{anthropic_action}','Cambiar estado de Anthropic?')" style="{_btn};color:{anthropic_color};border-color:{anthropic_color}">{anthropic_label}</button>
    <button onclick="alphaCmd('llm')" style="{_btn};color:#d29922;border-color:#d29922">📊 Ver costos LLM hoy</button>
    <button onclick="alphaCmd('health')" style="{_btn};color:#3fb950;border-color:#3fb950">🟢 Health snapshot</button>
  </div>

  <div style="margin-top:14px">
    <div style="font-size:.66rem;color:var(--mt);margin-bottom:6px;text-transform:uppercase;letter-spacing:.5px">PC remoto</div>
    <button onclick="alphaCmd('sleep','Poner la PC en sleep?')" style="{_btn};color:#6e7681;border-color:#6e7681">💤 Dormir PC (suspend)</button>
    <button onclick="alphaCmd('apagar','APAGAR la PC en 60s? (mandá cancelar para abortar)')" style="{_btn};color:#f85149;border-color:#f85149">🔴 Apagar PC (60s)</button>
    <button onclick="alphaCmd('cancelar')" style="{_btn};color:#d29922;border-color:#d29922">✋ Cancelar shutdown</button>
  </div>

  <pre id="cc-result" style="display:none;margin-top:12px;padding:10px;background:var(--s2);
       border-radius:4px;font-size:.72rem;color:var(--tx);white-space:pre-wrap;
       border-left:3px solid var(--ac);max-height:200px;overflow:auto"></pre>

  <div style="margin-top:12px;padding-top:8px;border-top:1px solid var(--bd);font-size:.66rem;color:var(--mt)">
    💡 Para que los botones funcionen, abrí <b>http://localhost:5050/dashboard</b> (no file://).
    El comando <code>apagar</code> da 60s de gracia.
  </div>
</div>"""


def _risk_band_panel(equity: float, baseline: float = 1600.0) -> str:
    """Iter7: card mostrando en qué banda de drawdown está el sistema AHORA.

    Refleja risk_action_for_drawdown de kelly.py — bandas escaladas que el
    monitor aplica automáticamente. Color-coded para detectar visual rápido.
    """
    if baseline <= 0:
        return ""
    dd_pct = (equity - baseline) / baseline * 100
    bands = [
        (0,    -2,  "NORMAL",       "#3fb950", "Operación normal — Kelly 1.0x"),
        (-2,   -4,  "REDUCE",       "#d29922", "Kelly 0.5x — sin entradas nuevas (reduce_mode.flag)"),
        (-4,   -6,  "CLOSE_LOSERS", "#ffa657", "Cerrar posiciones con P&L<0"),
        (-6,   -8,  "CLOSE_LONGS",  "#f85149", "Cerrar todos los longs equity, mantener hedge"),
        (-8, -100,  "KILL",         "#b91c1c", "KILL SWITCH — close_all_positions"),
    ]
    active_band = bands[0]
    for upper, lower, name, color, desc in bands:
        if dd_pct <= upper and dd_pct > lower:
            active_band = (upper, lower, name, color, desc)
            break
    if dd_pct < -8:
        active_band = bands[-1]

    name, color, desc = active_band[2], active_band[3], active_band[4]
    band_rows = []
    for upper, lower, bname, bcolor, bdesc in bands:
        is_active = (bname == name)
        bg = bcolor + "33" if is_active else "transparent"
        weight = "700" if is_active else "400"
        opacity = "1" if is_active else "0.55"
        band_rows.append(f"""
        <div style="display:flex;justify-content:space-between;padding:6px 10px;
                    background:{bg};opacity:{opacity};font-size:.74rem;
                    border-left:3px solid {bcolor}">
          <span style="font-weight:{weight};color:{bcolor};font-family:var(--mono)">
            {upper:+.0f}% → {lower:+.0f}% · {bname}
          </span>
          <span style="color:var(--mt)">{bdesc}</span>
        </div>""")
    rows_html = "\n".join(band_rows)

    return f"""<div class="card">
  <div class="card-head">
    <div>
      <div class="card-title">RISK BAND ACTIVA</div>
      <div class="card-sub">Drawdown intradía vs baseline ${baseline:.0f}</div>
    </div>
    <div style="text-align:right">
      <div style="font-family:var(--mono);font-size:1.6rem;font-weight:600;color:{color}">
        {name}
      </div>
      <div style="font-family:var(--mono);font-size:.84rem;color:{color}">
        {dd_pct:+.2f}%
      </div>
    </div>
  </div>
  {rows_html}
  <div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--bd);
              font-size:.7rem;color:var(--mt)">
    El monitor aplica esta lógica cada 30min. Cada cambio de banda notifica via WhatsApp/Telegram.
  </div>
</div>"""


def _regime_active_panel(signals_data: dict) -> str:
    """Iter7: card con régimen + parámetros de allocation activos hoy.

    Lee signals/latest.json (params + macro) para mostrar qué allocation
    aplicó hoy el analyst. Útil para confirmar que el modo agresivo iter3 está
    activo y para ver el cp_pct, n_cp, hold_days vigentes.
    """
    macro = signals_data.get("macro", {})
    params = signals_data.get("params", {})

    regime = (macro.get("regime", "unknown") or "unknown").upper()
    vix = macro.get("prices", {}).get("vix", 0) or 0

    cp_pct = (params.get("weight_short_term", 0) or 0) * 100
    opt_pct = (params.get("weight_options", 0) or 0) * 100
    n_cp = params.get("top_n_short_term", 0) or 0
    hold_days = params.get("max_hold_days_cp", 0) or 0

    color_map = {"BULL": "#3fb950", "BEAR": "#f85149", "LATERAL": "#d29922", "NEUTRAL": "#d29922"}
    rcolor = color_map.get(regime, "#6e7681")

    return f"""<div class="card">
  <div class="card-head">
    <div>
      <div class="card-title">RÉGIMEN + ALLOCATION ACTIVA</div>
      <div class="card-sub">Lo que decidió el analyst hoy · iter3 modo agresivo CP</div>
    </div>
    <div style="text-align:right">
      <div style="font-family:var(--mono);font-size:1.4rem;font-weight:700;color:{rcolor}">
        {regime}
      </div>
      <div style="font-family:var(--mono);font-size:.78rem;color:var(--mt)">
        VIX {vix:.1f}
      </div>
    </div>
  </div>
  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
              gap:10px;margin-top:8px">
    <div style="padding:10px;background:var(--s2);border-radius:4px">
      <div style="font-size:.62rem;color:var(--mt);text-transform:uppercase">CP sleeve</div>
      <div style="font-family:var(--mono);font-size:1.3rem;font-weight:600">{cp_pct:.0f}%</div>
      <div style="font-size:.66rem;color:var(--mt)">peso del capital</div>
    </div>
    <div style="padding:10px;background:var(--s2);border-radius:4px">
      <div style="font-size:.62rem;color:var(--mt);text-transform:uppercase">OPT sleeve</div>
      <div style="font-family:var(--mono);font-size:1.3rem;font-weight:600">{opt_pct:.0f}%</div>
      <div style="font-size:.66rem;color:var(--mt)">opciones long</div>
    </div>
    <div style="padding:10px;background:var(--s2);border-radius:4px">
      <div style="font-size:.62rem;color:var(--mt);text-transform:uppercase">Top picks</div>
      <div style="font-family:var(--mono);font-size:1.3rem;font-weight:600">{n_cp}</div>
      <div style="font-size:.66rem;color:var(--mt)">concentración</div>
    </div>
    <div style="padding:10px;background:var(--s2);border-radius:4px">
      <div style="font-size:.62rem;color:var(--mt);text-transform:uppercase">Hold máx</div>
      <div style="font-family:var(--mono);font-size:1.3rem;font-weight:600">{hold_days}d</div>
      <div style="font-size:.66rem;color:var(--mt)">rotación</div>
    </div>
  </div>
</div>"""


def _orphan_positions_panel(positions, signals_data: dict) -> str:
    """Iter7: card con posiciones huérfanas — abiertas en Alpaca pero SIN signal.

    Identifica posiciones que están en el broker pero no tienen señal activa
    en signals/latest.json. Estas no se gestionan (no se les aplica trailing,
    no tienen stop dinámico). Son el riesgo más concreto de pérdida hoy.
    """
    if not positions:
        return ""
    tickers_with_signal = set()
    for bucket in ("long_term", "short_term", "options_book", "hedge_book"):
        for s in signals_data.get(bucket, []):
            t = s.get("ticker", "")
            if t:
                tickers_with_signal.add(t)

    orphans = []
    for p in positions:
        is_option = any(c in p.ticker for c in ("C0", "P0")) and len(p.ticker) > 10
        if is_option:
            continue
        if p.ticker not in tickers_with_signal:
            orphans.append(p)

    if not orphans:
        return ""

    # Iter8: bot username para botones interactivos. Hardcoded por ahora —
    # idealmente leer de config. NAF usa @sfelix23_alpha_bot (chequear si cambia).
    import os as _os_iter8
    _bot_username = _os_iter8.getenv("TELEGRAM_BOT_USERNAME", "sfelix23_alpha_bot")

    rows = []
    total_pnl = 0.0
    for o in sorted(orphans, key=lambda x: -float(x.unrealized_pl)):
        pnl = float(o.unrealized_pl)
        cost = float(o.avg_price) * float(o.qty)
        pct = (pnl / cost * 100) if cost else 0
        total_pnl += pnl
        color = "#3fb950" if pnl >= 0 else "#f85149"

        # Iter8: 3 acciones diferentes segun el P&L
        if pnl > 20:
            rec = "✅ Winner — auto-mantener"
            btn = ""
        elif pnl < -5:
            rec = "🔴 Auto-handler la cerrara en proxima corrida"
            # Link al bot con comando liquidate <ticker> pre-armado
            btn = (f'<a href="https://t.me/{_bot_username}?text=liquidate%20{o.ticker}" '
                   f'target="_blank" style="display:inline-block;margin-top:4px;'
                   f'padding:4px 10px;background:#f8514922;color:#f85149;'
                   f'border:1px solid #f85149;border-radius:3px;font-size:.66rem;'
                   f'text-decoration:none;font-family:var(--mono)">⚡ LIQUIDAR YA</a>')
        else:
            rec = "⚠️ Zona neutra — monitoreando"
            btn = (f'<a href="https://t.me/{_bot_username}?text=liquidate%20{o.ticker}" '
                   f'target="_blank" style="display:inline-block;margin-top:4px;'
                   f'padding:4px 10px;background:#d2992222;color:#d29922;'
                   f'border:1px solid #d29922;border-radius:3px;font-size:.66rem;'
                   f'text-decoration:none;font-family:var(--mono)">⚡ Liquidar manual</a>')

        rows.append(f"""
        <div style="display:flex;justify-content:space-between;padding:8px 0;
                    border-bottom:1px solid var(--bd);font-size:.78rem">
          <div style="flex:1">
            <div style="font-weight:700;font-family:var(--mono)">{o.ticker}</div>
            <div style="font-size:.66rem;color:var(--mt)">
              {o.qty:.4f} shares · cost ${cost:.0f}
            </div>
          </div>
          <div style="text-align:right;flex:1">
            <div style="font-family:var(--mono);color:{color}">
              ${pnl:+.2f} ({pct:+.1f}%)
            </div>
            <div style="font-size:.66rem;color:var(--mt)">{rec}</div>
            {btn}
          </div>
        </div>""")
    rows_html = "\n".join(rows)
    total_color = "#3fb950" if total_pnl >= 0 else "#f85149"

    # Iter8: bot actions globales
    _btn_style = ("display:inline-block;margin:4px 6px 0 0;padding:6px 12px;"
                  "border-radius:4px;font-size:.72rem;text-decoration:none;"
                  "font-family:var(--mono);border:1px solid")
    bot_actions = f"""
    <div style="margin-top:12px;padding-top:10px;border-top:1px solid var(--bd)">
      <div style="font-size:.66rem;color:var(--mt);margin-bottom:6px;text-transform:uppercase;letter-spacing:.5px">
        Acciones rapidas (abren Telegram con comando pre-armado)
      </div>
      <a href="https://t.me/{_bot_username}?text=liquidate%20orphans" target="_blank"
         style="{_btn_style};background:#f8514922;color:#f85149;border-color:#f85149">
        🔴 Liquidar todas las huerfanas negativas
      </a>
      <a href="https://t.me/{_bot_username}?text=cartera" target="_blank"
         style="{_btn_style};background:#3b82f622;color:#3b82f6;border-color:#3b82f6">
        📊 Ver cartera completa
      </a>
      <a href="https://t.me/{_bot_username}?text=health" target="_blank"
         style="{_btn_style};background:#3fb95022;color:#3fb950;border-color:#3fb950">
        🟢 Health snapshot
      </a>
    </div>
    """

    return f"""<div class="card" style="border-left:3px solid #d29922">
  <div class="card-head">
    <div>
      <div class="card-title">⚠️ POSICIONES HUÉRFANAS ({len(orphans)})</div>
      <div class="card-sub">Sin signal activa · Iter8: auto-handler las gestiona ahora</div>
    </div>
    <div style="text-align:right">
      <div style="font-family:var(--mono);font-size:1.2rem;font-weight:600;color:{total_color}">
        ${total_pnl:+.2f}
      </div>
      <div style="font-size:.7rem;color:var(--mt)">P&L total huérfanas</div>
    </div>
  </div>
  {rows_html}
  <div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--bd);
              font-size:.7rem;color:var(--mt)">
    <b>Reglas auto-handler (cada 30min):</b><br>
    🟢 P&L > +20% → MANTENER (winner) · 🔴 P&L < -5% → AUTO-CLOSE · 🟡 Hold > 7d sin progreso → CLOSE
  </div>
  {bot_actions}
</div>"""


def _llm_status_panel() -> str:
    """Iter6: card con estado del LLM gateway — providers, calls, costo.

    Lee signals/llm_budget.json y signals/llm_provider_state.json (commiteados
    por _push_results desde iter6). Si Anthropic está OFF (ENABLE_ANTHROPIC
    no seteada), lo muestra explícito con el botón para activarlo.
    """
    import json, os
    from datetime import datetime, timezone

    budget = {}
    state  = {"disabled": {}}
    try:
        b_path = BASE_DIR / "signals" / "llm_budget.json"
        if b_path.exists():
            budget = json.loads(b_path.read_text(encoding="utf-8"))
    except Exception:
        pass
    try:
        s_path = BASE_DIR / "signals" / "llm_provider_state.json"
        if s_path.exists():
            state = json.loads(s_path.read_text(encoding="utf-8"))
    except Exception:
        pass

    providers_meta = [
        ("groq",       "Groq",       "Llama 3.3 70B"),
        ("gemini",     "Gemini",     "Flash 2.0"),
        ("deepseek",   "DeepSeek",   "Chat / R1"),
        ("openrouter", "OpenRouter", "Qwen 2.5 / Llama"),
        ("anthropic",  "Anthropic",  "Haiku 4.5"),
    ]

    rows = []
    total_calls = 0
    total_cost = 0.0
    cache_hits = 0
    for pid, name, model in providers_meta:
        p = budget.get("providers", {}).get(pid, {})
        calls = p.get("calls", 0)
        cost  = p.get("cost_usd", 0.0)
        hits  = p.get("cache_hits", 0)
        total_calls += calls
        total_cost  += cost
        cache_hits  += hits

        disabled_entry = state.get("disabled", {}).get(pid)
        if pid == "anthropic":
            anthropic_on = os.getenv("ENABLE_ANTHROPIC", "").lower() in ("true", "1", "yes")
            if not anthropic_on:
                status_html = '<span style="color:#6e7681">OFF (flag)</span>'
            elif disabled_entry:
                status_html = f'<span style="color:#f85149">AUTO-DISABLED</span>'
            else:
                status_html = '<span style="color:#3fb950">ON</span>'
        else:
            if disabled_entry:
                status_html = '<span style="color:#f85149">AUTO-DISABLED</span>'
            else:
                status_html = '<span style="color:#3fb950">activo</span>'

        rows.append(f"""
        <div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--bd);font-size:.78rem">
          <div style="flex:1">
            <div style="font-weight:600;color:var(--tx)">{name}</div>
            <div style="font-size:.68rem;color:var(--mt)">{model}</div>
          </div>
          <div style="text-align:right">
            <div style="font-family:var(--mono)">{calls} calls · ${cost:.4f}</div>
            <div style="font-size:.68rem">{status_html}</div>
          </div>
        </div>
        """)

    rows_html = "".join(rows)
    cache_pct = (cache_hits / max(total_calls, 1)) * 100 if total_calls else 0
    today = budget.get("date", "—")

    return f"""<div class="card">
  <div class="card-head">
    <div>
      <div class="card-title">LLM GATEWAY — STATUS</div>
      <div class="card-sub">5 providers · cascada free-first · Anthropic OFF por flag (anti-flag)</div>
    </div>
    <div style="text-align:right">
      <div style="font-family:var(--mono);font-size:.9rem;color:var(--ac)">{total_calls} calls</div>
      <div style="font-family:var(--mono);font-size:.74rem;color:var(--mt)">${total_cost:.4f} · {cache_pct:.0f}% cache hit</div>
      <div style="font-size:.66rem;color:var(--mt)">fecha: {today}</div>
    </div>
  </div>
  {rows_html}
  <div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--bd);font-size:.7rem;color:var(--mt)">
    Para activar Anthropic temporalmente:
    <code>gcloud run jobs update alpha-daily --update-env-vars ENABLE_ANTHROPIC=true --region us-central1 --project alpha-agent-2025</code>
  </div>
</div>"""


def _workflow_health_panel(status: dict) -> str:
    """Mini-panel con estado de los workflows de GitHub Actions — con fetch live vía JS."""
    from datetime import datetime, timezone

    workflows = [
        ("alpha_daily",      "Alpha Daily",      "Analyst + Trader LP/CP · 10:40 ART"),
        ("alpha_daytrader",  "Alpha DayTrader",  "Day Trader ORB · 11:15 ART · cuenta DT"),
        ("alpha_monitor",    "Alpha Monitor",    "Stops/TPs cada 30 min · 11:00-16:00 ART"),
        ("alpha_weekly",     "Alpha Weekly",     "Rebalancer semanal · viernes 15:00 ART"),
    ]

    now_utc = datetime.now(timezone.utc)

    def _pill(key, label, desc):
        entry = status.get(key, {})
        ts    = entry.get("ts", "")
        ok    = entry.get("ok", None)
        if not ts:
            color, icon, age_str = "#6e7681", "○", "nunca corrió"
        else:
            try:
                dt  = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
                age = (now_utc - dt).total_seconds() / 3600
                age_str = f"hace {age:.0f}h" if age >= 1 else f"hace {int(age*60)}min"
            except Exception:
                age_str = ts[:16]
            if ok is True:
                color, icon = "#3fb950", "✓"
            elif ok is False:
                color, icon = "#f85149", "✗"
            else:
                color, icon = "#d29922", "?"

        return f"""<div style="display:flex;align-items:center;gap:10px;padding:8px 0;
  border-bottom:1px solid var(--bd)" id="wf-row-{key}">
  <span style="font-size:1rem;color:{color};width:16px;text-align:center;flex-shrink:0"
        id="wf-icon-{key}">{icon}</span>
  <div style="flex:1;min-width:0">
    <div style="font-size:.82rem;font-weight:700;color:var(--tx)">{label}</div>
    <div style="font-size:.72rem;color:var(--mt);white-space:nowrap;overflow:hidden;
                text-overflow:ellipsis" id="wf-desc-{key}">{desc}</div>
  </div>
  <div style="font-size:.72rem;flex-shrink:0" id="wf-age-{key}"
       style="color:{color}">{age_str}</div>
</div>"""

    pills = "".join(_pill(k, l, d) for k, l, d in workflows)

    # JavaScript que actualiza el panel con datos live de Cloud Run (via workflow_status.json)
    live_js = """<script>
(async function() {
  try {
    const r = await fetch(
      'https://raw.githubusercontent.com/sfelix23/alpha-agent/master/signals/workflow_status.json',
      { cache: 'no-cache' }
    );
    if (!r.ok) return;
    const data = await r.json();
    const now = Date.now();
    const keys = ['alpha_daily', 'alpha_daytrader', 'alpha_monitor', 'alpha_weekly'];
    for (const key of keys) {
      const entry = data[key];
      if (!entry) continue;
      const iconEl = document.getElementById('wf-icon-' + key);
      const ageEl  = document.getElementById('wf-age-'  + key);
      if (!iconEl || !ageEl) continue;
      const ok    = entry.ok === true;
      const failed = entry.ok === false;
      const color = ok ? '#3fb950' : failed ? '#f85149' : '#d29922';
      const icon  = ok ? '✓' : failed ? '✗' : '?';
      let ageStr = entry.ts || '?';
      try {
        const ageMs = now - new Date(entry.ts + 'Z').getTime();
        const ageH  = ageMs / 3600000;
        ageStr = ageH < 1 ? `hace ${Math.round(ageH * 60)}min` : `hace ${ageH.toFixed(0)}h`;
      } catch(e) {}
      iconEl.textContent = icon;
      iconEl.style.color = color;
      ageEl.textContent  = ageStr;
      ageEl.style.color  = color;
    }
    const srcEl = document.getElementById('wf-source');
    if (srcEl) {
      srcEl.textContent = '· Live desde Cloud Run ✓';
      srcEl.style.color = '#3fb950';
    }
  } catch(e) {
    const srcEl = document.getElementById('wf-source');
    if (srcEl) srcEl.textContent = '· (datos en caché — sin acceso a repo)';
  }
})();
</script>"""

    return f"""<div class="card" style="min-width:260px">
  <div class="card-head" style="margin-bottom:8px">
    <div class="card-title">Google Cloud Run &mdash; Estado</div>
    <div class="card-sub">
      <a href="https://console.cloud.google.com/run/jobs?project=alpha-agent-2025" target="_blank"
         style="color:#58a6ff;text-decoration:none">alpha-agent-2025</a>
    </div>
  </div>
  {pills}
  <div style="font-size:.68rem;color:var(--mt);margin-top:8px" id="wf-source">
    · datos en caché (actualizando...)
  </div>
  <div style="font-size:.68rem;color:var(--mt);margin-top:2px">
    El scalper corre localmente &mdash; no en la nube.
  </div>
</div>
{live_js}"""


# ─── TAB: RESUMEN ─────────────────────────────────────────────────────────────

def _tres_cuentas_panel(lp_trades: list, dt_trades: list, scalp_trades: list) -> str:
    """Tarjeta resumen de las 3 cuentas Alpaca separadas."""
    def _account_stats(trades: list, label: str, color: str, budget: float, description: str) -> str:
        closed  = [t for t in trades if t.get("side","BUY").upper() in ("BUY","SELL") and t.get("closed_at")]
        n       = len(closed)
        wins    = [t for t in closed if (t.get("pnl_usd") or 0) > 0]
        total_p = sum(t.get("pnl_usd") or 0 for t in closed)
        wr      = len(wins)/n*100 if n else None
        pc      = _c(total_p)
        wr_c    = "#3fb950" if (wr or 0) > 55 else "#d29922" if (wr or 0) > 45 else "#f85149"
        pnl_str = (("+" if total_p >= 0 else "") + _usd(total_p)) if n else "—"
        wr_str  = f"{wr:.1f}%" if wr is not None else "—"
        return f"""
<div style="flex:1;min-width:200px;padding:16px;border:1px solid {color}33;
  border-left:3px solid {color};border-radius:8px;background:var(--bg)">
  <div style="font-size:.72rem;font-weight:700;color:{color};text-transform:uppercase;
    letter-spacing:.06em;margin-bottom:8px">{label}</div>
  <div style="font-size:1.1rem;font-weight:700;color:{pc};margin-bottom:4px">{pnl_str}</div>
  <div style="font-size:.75rem;color:var(--mt);margin-bottom:6px">{description}</div>
  <div style="display:flex;gap:12px;font-size:.75rem">
    <div><span style="color:var(--mt)">Win rate</span>&nbsp;
      <span style="color:{wr_c};font-weight:700">{wr_str}</span></div>
    <div><span style="color:var(--mt)">Trades</span>&nbsp;
      <span style="color:var(--tx)">{n}</span></div>
  </div>
</div>"""

    lp_block    = _account_stats(lp_trades,    "LP / CP",  "#f59e0b", 1600.0, "ALPACA_API_KEY · hold semanal")
    dt_block    = _account_stats(dt_trades,    "Day Trade", "#818cf8", 1600.0, "ALPACA_DT_API_KEY · $1400/trade")
    scalp_block = _account_stats(scalp_trades, "Scalping",  "#34d399", 1600.0, "ALPACA_SCALP_API_KEY · $400/trade")

    return f"""<div class="card">
  <div class="card-head" style="margin-bottom:12px">
    <div class="card-title">Las 3 Cuentas — Comparativa</div>
    <div class="card-sub">Cuentas Alpaca paper independientes &middot; P&L realizado total</div>
  </div>
  <div style="display:flex;gap:12px;flex-wrap:wrap">
    {lp_block}{dt_block}{scalp_block}
  </div>
</div>"""


def _tab_resumen(equity, initial, regime, vix, wti, gold, dxy,
                 history, spy_history, signals_data, metrics, age_hours,
                 perf_data=None, qqq_history=None, mc_result=None,
                 tres_cuentas_html="", wf_health_html="", llm_status_html="",
                 risk_band_html="", regime_active_html="", orphan_html="",
                 control_center_html="", adv_metrics_html=""):
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
    alpha_v   = metrics.get("alpha_1m")
    spy_r     = metrics.get("spy_ret_1m")
    port_r    = metrics.get("port_ret_1m", 0) or 0
    arr_v     = metrics.get("arr", 0) or 0
    win_rate  = metrics.get("win_rate", 0) or 0

    spy_badge = ""
    if spy_r is not None:
        spy_badge = f'&nbsp;&middot;&nbsp;<span style="color:#7d8590">SPY {_pct(spy_r)}</span>'

    # Alpha KPI: el número más importante — ¿le ganamos al índice?
    if alpha_v is not None:
        alpha_color = "#3fb950" if alpha_v > 0 else "#f85149"
        alpha_kpi = f"""
  <div class="kpi" style="border-left:3px solid {alpha_color};padding-left:12px">
    <div class="kpi-lbl">Alpha vs SPY (1M)</div>
    <div class="kpi-val" style="color:{alpha_color}">{_pct(alpha_v)}</div>
    <div class="kpi-sub">Port {_pct(port_r)} · SPY {_pct(spy_r)}</div>
  </div>"""
    else:
        alpha_kpi = ""

    kpis = f"""
<div class="kpi-row">
  <div class="kpi kpi-hero" data-countup="{equity:.2f}">
    <div class="kpi-lbl">Patrimonio Total</div>
    <div class="kpi-val kpi-hero-val" style="color:{pc}" id="kpi-equity">{_usd(equity)}</div>
    <div class="kpi-tag" style="background:{pbg};color:{pc}">{_usd(pnl)} &nbsp; {_pct(pnl_pct)} total{spy_badge}</div>
  </div>
  {alpha_kpi}
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
  {control_center_html}
  <div class="section-gap"></div>
  {orphan_html}
  <div class="section-gap"></div>
  {risk_band_html}
  <div class="section-gap"></div>
  {regime_active_html}
  <div class="section-gap"></div>
  {adv_metrics_html}
  <div class="section-gap"></div>
  {tres_cuentas_html}
  <div class="section-gap"></div>
  {wf_health_html}
  <div class="section-gap"></div>
  {llm_status_html}
  <div class="section-gap"></div>
  {eq_chart}
  <div class="section-gap"></div>
  {cal}
  <div class="section-gap"></div>
  {_perf_chart(perf_data)}
  <div class="section-gap"></div>
  {_edgar_alerts_panel(signals_data)}
  <div class="section-gap"></div>
  {_monte_carlo_panel(mc_result)}
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
    from collections import defaultdict
    from datetime import date

    # Agrupar por fecha y usar el último equity del día (el monitor corre cada 30 min,
    # así que hay múltiples entradas por día). Comparar cierre-a-cierre entre días.
    by_date: dict = defaultdict(list)
    for entry in history:
        try:
            d = datetime.fromtimestamp(entry["ts"]).date()
            eq = float(entry["equity"])
            if eq > 0:
                by_date[d].append(eq)
        except Exception:
            continue

    sorted_dates = sorted(by_date.keys())
    daily = {}
    for i in range(1, len(sorted_dates)):
        prev_eq = by_date[sorted_dates[i - 1]][-1]   # último equity del día anterior
        curr_eq = by_date[sorted_dates[i]][-1]        # último equity del día actual
        pnl = curr_eq - prev_eq
        pnl_pct = (pnl / prev_eq * 100) if prev_eq else 0
        daily[sorted_dates[i]] = {"pnl": pnl, "pct": pnl_pct}

    # Siempre mostrar el mes actual, aunque no haya datos aún para él
    today = date.today()
    yr, mo = today.year, today.month
    month_name = ["Enero","Febrero","Marzo","Abril","Mayo","Junio",
                  "Julio","Agosto","Septiembre","Octubre","Noviembre","Diciembre"][mo-1]

    month_d = {d:v for d,v in daily.items() if d.year==yr and d.month==mo}
    total_pct = sum(v["pct"] for v in month_d.values())
    total_pnl = sum(v["pnl"] for v in month_d.values())
    wins    = sum(1 for v in month_d.values() if v["pct"]>=0)
    losses  = len(month_d) - wins
    best    = max(month_d.values(), key=lambda v: v["pct"], default=None)
    worst   = min(month_d.values(), key=lambda v: v["pct"], default=None)

    def cell_style(pct):
        if pct > 0:
            i = min(pct / 2.0, 1.0)   # satura en +2%
            r = int(10 + 30*i); g = int(55 + 130*i); b = int(25 + 30*i)
            return f"background:rgb({r},{g},{b})", "#ffffff"
        elif pct < 0:
            i = min(abs(pct) / 2.0, 1.0)  # satura en -2%
            r = int(55 + 170*i); g = int(15 + 20*i); b = int(15 + 20*i)
            return f"background:rgb({r},{g},{b})", "#ffffff"
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
                    bg, tc2 = cell_style(data["pct"])
                    s = "+" if data["pct"]>=0 else ""
                    rows += f"""<td class="cal-cell" style="{bg}">
  <span class="cal-n">{day_n}</span>
  <span class="cal-p" style="color:{tc2}">{s}{data['pct']:.2f}%</span>
  <span class="cal-q" style="color:{tc2}">{s}${data['pnl']:,.0f}</span>
</td>"""
                elif dow >= 5:
                    rows += f'<td class="cal-cell cal-wk"><span class="cal-n">{day_n}</span></td>'
                else:
                    rows += f'<td class="cal-cell cal-nd"><span class="cal-n">{day_n}</span><span class="cal-q">—</span></td>'
            day_n += 1
        rows += "</tr>"

    bh = f'<b style="color:#3fb950">+{best["pct"]:.2f}%</b>' if best else "—"
    wh = f'<b style="color:#f85149">{worst["pct"]:.2f}%</b>' if worst else "—"
    tc_total = _c(total_pnl)

    return f"""
<div class="card">
  <div class="card-head">
    <div>
      <div class="card-title">Resultados Diarios — {month_name} {yr}</div>
      <div class="card-sub">{wins} dias positivos &middot; {losses} negativos &middot; Mejor: {bh} &middot; Peor: {wh}</div>
    </div>
    <div class="pill" style="color:{tc_total};font-size:1rem;font-weight:700">{"+" if total_pct>=0 else ""}{total_pct:.2f}%</div>
  </div>
  <div class="cal-wrap">
    <table class="cal-table">
      <thead><tr>{"".join(f'<th class="cal-th">{d}</th>' for d in ["Lun","Mar","Mie","Jue","Vie","Sab","Dom"])}</tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
</div>"""


# ─── TAB: POSICIONES ──────────────────────────────────────────────────────────

def _tab_posiciones(positions, signals_data=None):
    if not positions:
        return """
<div class="tab-content" id="tab-posiciones">
  <div class="card"><p class="muted" style="padding:60px;text-align:center;font-size:1rem">
    Sin posiciones abiertas en este momento.
  </p></div>
</div>"""

    # iter15: lookup de stop/TP (desde signals) + fecha de entrada (desde trade_db)
    # para mostrar riesgo por posición: distancia al stop, $ en riesgo, días en hold.
    _stops: dict[str, dict] = {}
    for _bucket in ("long_term", "short_term"):
        for _s in (signals_data or {}).get(_bucket, []):
            _t = _s.get("ticker")
            if _t:
                _stops[_t] = {"stop": _s.get("stop_loss"), "tp": _s.get("take_profit")}
    _entry_dates: dict[str, str] = {}
    try:
        from alpha_agent.analytics.trade_db import get_trades as _gt
        for _tr in _gt(limit=200):
            if _tr.get("side") == "BUY" and not _tr.get("closed_at"):
                _tk = _tr.get("ticker")
                if _tk and _tk not in _entry_dates:
                    _entry_dates[_tk] = (_tr.get("ts") or _tr.get("date") or "")
    except Exception:
        pass

    def _risk_row(p) -> str:
        info = _stops.get(p.ticker, {})
        stop = info.get("stop")
        tp = info.get("tp")
        px = p.market_value / p.qty if (p.qty or 0) else (p.avg_price or 0)
        bits = []
        if stop and px:
            dist = (px - stop) / px * 100
            at_risk = max(0.0, (px - stop) * (p.qty or 0))
            dcol = "#f85149" if dist < 4 else ("#d29922" if dist < 8 else "#3fb950")
            bits.append(f'<span>Stop <b style="color:#f85149">{_usd(stop)}</b></span>')
            bits.append(f'<span>Dist. stop <b style="color:{dcol}">{dist:+.1f}%</b></span>')
            bits.append(f'<span>$ en riesgo <b>{_usd(at_risk)}</b></span>')
        if tp:
            bits.append(f'<span>TP <b style="color:#3fb950">{_usd(tp)}</b></span>')
        ed = _entry_dates.get(p.ticker, "")
        if ed:
            try:
                from datetime import datetime as _dt
                days = (_dt.now() - _dt.fromisoformat(ed[:19])).total_seconds() / 86400
                bits.append(f'<span>Hold <b>{days:.0f}d</b></span>')
            except Exception:
                pass
        if not bits:
            bits.append('<span class="muted">sin stop en señal (huérfana)</span>')
        return ('<div class="pos-meta" style="margin-top:4px;border-top:1px dashed var(--bd);padding-top:5px">'
                + "".join(bits) + "</div>")

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
  {_risk_row(p)}
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


def _monte_carlo_panel(mc) -> str:
    if mc is None:
        return ""
    def _mc_cell(label, value, color="#e6edf3"):
        return (
            f'<div class="mc-cell">'
            f'<div class="mc-label">{label}</div>'
            f'<div class="mc-val" style="color:{color}">{value}</div>'
            f'</div>'
        )
    cells = "".join([
        _mc_cell("Retorno Mediano", f"{mc.median_return_pct:+.1f}%",
                 "#22c55e" if mc.median_return_pct >= 0 else "#ef4444"),
        _mc_cell("P10 (pesimista)", f"{mc.p10_return_pct:+.1f}%", "#ef4444"),
        _mc_cell("P90 (optimista)", f"{mc.p90_return_pct:+.1f}%", "#22c55e"),
        _mc_cell("DD Máx Esperado", f"{mc.expected_max_dd_pct:.1f}%", "#f59e0b"),
        _mc_cell("DD Máx Worst 95%", f"{mc.worst_case_max_dd_pct:.1f}%", "#ef4444"),
        _mc_cell("VaR 95% Diario", f"{mc.var_95_daily_pct:.2f}%", "#ef4444"),
        _mc_cell("Prob. Positivo", f"{mc.prob_positive_pct:.0f}%",
                 "#22c55e" if mc.prob_positive_pct >= 50 else "#ef4444"),
        _mc_cell("Prob. Vencer SPY", f"{mc.prob_beat_spy_pct:.0f}%",
                 "#22c55e" if mc.prob_beat_spy_pct >= 50 else "#f59e0b"),
        _mc_cell("Capital Mediano", f"${mc.median_final_capital:,.0f}", "#e6edf3"),
        _mc_cell("Peor caso (5%)", f"${mc.worst_case_capital:,.0f}", "#ef4444"),
    ])
    return f"""<div class="card">
  <div class="card-head">
    <div>
      <div class="card-title">Monte Carlo — Simulación de Riesgo</div>
      <div class="card-sub">{mc.n_simulations:,} simulaciones · horizonte {mc.horizon_days} días · basado en historial real</div>
    </div>
  </div>
  <div class="mc-grid">{cells}</div>
</div>"""


def _edgar_alerts_panel(signals_data: dict) -> str:
    alerts = signals_data.get("edgar_alerts", [])
    if not alerts:
        return ""
    rows = []
    for a in alerts[:6]:
        ticker    = a.get("ticker", "")
        sentiment = a.get("sentiment", "neutral").lower()
        summary   = a.get("summary", "")
        impact    = a.get("impact_pct", 0)
        date_str  = a.get("filing_date", "")[:10]
        css_cls   = "edgar-bull" if sentiment == "bullish" else "edgar-bear" if sentiment == "bearish" else "edgar-neutral"
        impact_str = f" · impacto estimado {impact:+.1f}%" if impact else ""
        rows.append(
            f'<div class="edgar-alert {css_cls}">'
            f'<strong>{ticker}</strong> &nbsp;<span style="opacity:.6;font-size:.72rem">{date_str}</span>'
            f'<div style="margin-top:3px">{summary}{impact_str}</div>'
            f'</div>'
        )
    return f"""<div class="card">
  <div class="card-head">
    <div>
      <div class="card-title">EDGAR 8-K — Eventos Materiales</div>
      <div class="card-sub">Filings SEC de las últimas 48h analizados por Claude</div>
    </div>
  </div>
  {''.join(rows)}
</div>"""


def _perf_chart(perf_data: dict | None) -> str:
    """Gráfico de barras agrupadas: portfolio vs SPY semana a semana."""
    weeks = (perf_data or {}).get("weeks", [])
    if not weeks:
        return '<div class="card"><div class="card-head"><div><div class="card-title">Performance Semanal vs SPY</div><div class="card-sub">Disponible tras el primer rebalanceo del viernes</div></div></div><p class="muted" style="padding:8px 0 12px">Sin historial de performance semanal todavia.</p></div>'

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
        y:{{ticks:{{color:'#7d8590',callback:v=>parseFloat(v.toFixed(1))+'%',font:{{size:10}}}},grid:{{color:'#21262d'}}}}
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

def _swarm_inline(swarm: dict | None) -> str:
    """Mini-bloque del debate Swarm para cada señal LP/CP en el tab Señales."""
    if not swarm:
        return ""
    go       = swarm.get("go", False)
    size_f   = swarm.get("size_factor", 1.0)
    go_count = swarm.get("go_count", 0)
    ev_val   = swarm.get("ev", 0)
    reasoning = _esc(swarm.get("reasoning", "")[:200])
    fin_c    = "#3fb950" if go else "#f85149"
    ev_c     = "#3fb950" if ev_val >= 0 else "#f85149"
    ev_sign  = "+" if ev_val >= 0 else ""

    agent_icons = {"QuantAnalyst": "Q", "TechnicalCP": "T", "MacroLP": "M",
                   "MacroCP": "M", "SentimentLP": "N", "SentimentCP": "N",
                   "RiskAuditorLP": "R"}
    agent_colors = {"QuantAnalyst": "#818cf8", "TechnicalCP": "#34d399",
                    "MacroLP": "#60a5fa", "MacroCP": "#60a5fa",
                    "SentimentLP": "#fbbf24", "SentimentCP": "#fbbf24",
                    "RiskAuditorLP": "#f87171"}
    stance_colors = {"GO": "#3fb950", "NO-GO": "#f85149", "REDUCE": "#d29922"}

    pills = ""
    for op in swarm.get("opinions", []):
        ag    = op.get("agent", "?")
        stance = op.get("stance", "?")
        conf  = op.get("confidence", 0)
        ic    = agent_colors.get(ag, "#8b949e")
        sc    = stance_colors.get(stance, "#8b949e")
        icon  = agent_icons.get(ag, "?")
        cot   = op.get("cot", "").strip()
        tip   = _esc(cot[:180]) if cot else _esc(op.get("reasoning", "")[:120])
        pills += (
            f'<span title="{tip}" style="display:inline-flex;align-items:center;gap:4px;'
            f'font-size:.72rem;padding:2px 7px;border-radius:4px;cursor:help;'
            f'background:{ic}15;border:1px solid {ic}44">'
            f'<span style="color:{ic};font-weight:700">{icon}</span>'
            f'<span style="color:{sc};font-weight:700">{stance}</span>'
            f'<span style="color:var(--mt)">{conf}%</span></span> '
        )

    return f"""<div style="margin-top:12px;padding:10px 12px;border-radius:6px;
  background:var(--bg);border:1px solid var(--br)">
  <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px">
    <span style="font-size:.72rem;font-weight:700;color:#818cf8">SWARM</span>
    <span style="font-size:.72rem;font-weight:700;color:{fin_c};background:{fin_c}15;
      padding:1px 7px;border-radius:4px">{'GO' if go else 'NO-GO'} &times;{size_f}</span>
    <span style="font-size:.72rem;color:#8b949e">{go_count}/4 GO</span>
    <span style="font-size:.72rem;color:{ev_c};background:{ev_c}15;padding:1px 6px;
      border-radius:4px">EV {ev_sign}${abs(ev_val):.0f}</span>
  </div>
  <div style="margin-bottom:6px">{pills}</div>
  <div style="font-size:.73rem;color:var(--mt)">{reasoning}</div>
</div>"""


def _tab_senales(signals_data, positions=None):
    if not signals_data:
        return '<div class="tab-content" id="tab-senales"><div class="card"><p class="muted" style="padding:40px">Sin senales disponibles.</p></div></div>'

    gen_at = signals_data.get("generated_at","")[:16].replace("T"," ")
    body   = ""

    # iter15: estado de ejecución por señal (ejecutada vs pendiente/skip + motivo)
    _held = {p.ticker for p in (positions or []) if (p.market_value or 0) > 1.0}
    _stopouts = set()
    _mem_bias: dict[str, str] = {}
    try:
        from alpha_agent.analytics.trade_db import get_recent_stopouts, get_ticker_memory
        _stopouts = get_recent_stopouts(hours=36)
    except Exception:
        get_ticker_memory = None  # type: ignore

    def _exec_badge(tk: str) -> str:
        if tk in _held:
            return '<span style="font-size:.66rem;padding:2px 7px;border-radius:4px;background:#3fb95022;color:#3fb950;border:1px solid #3fb95055">✓ EJECUTADA</span>'
        reason = "pendiente / sin BP"
        if tk in _stopouts:
            reason = "stop-out cooldown 36h"
        else:
            try:
                if get_ticker_memory:
                    m = get_ticker_memory(tk)
                    if m.get("bias") == "adverso":
                        reason = "memoria adversa"
            except Exception:
                pass
        return f'<span style="font-size:.66rem;padding:2px 7px;border-radius:4px;background:#d2992222;color:#d29922;border:1px solid #d2992255">⏳ {reason}</span>'

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
      {_exec_badge(ticker)}
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
    {_swarm_inline(th.get("swarm"))}
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

    # iter15: memoria expandida — tabla por ticker (segundo cerebro completo)
    mem_html = ""
    try:
        from alpha_agent.analytics.trade_db import _conn, get_ticker_memory
        with _conn() as _con:
            _tks = [r["ticker"] for r in _con.execute(
                "SELECT ticker, COUNT(*) c FROM trades WHERE side='BUY' AND closed_at IS NOT NULL "
                "GROUP BY ticker ORDER BY c DESC"
            ).fetchall()]
        rows = ""
        for tk in _tks:
            m = get_ticker_memory(tk)
            if not m["n"]:
                continue
            bcol = {"favorable": "#3fb950", "adverso": "#f85149"}.get(m["bias"], "#8b949e")
            wr = m["win_rate"]; ap = m["avg_pnl_pct"]
            rows += (f"<tr><td class='rt-ticker'>{tk}</td>"
                     f"<td>{m['n']}</td>"
                     f"<td style='color:{'#3fb950' if (wr or 0)>=0.5 else '#f85149'}'>{(wr*100):.0f}%</td>"
                     f"<td style='color:{'#3fb950' if (ap or 0)>=0 else '#f85149'}'>{ap:+.1f}%</td>"
                     f"<td>{m['avg_hold_days']:.0f}d</td>"
                     f"<td style='color:{bcol};font-weight:700'>{m['bias'].upper()}</td></tr>")
        if rows:
            mem_html = f"""
  <div class="card" style="margin-top:18px">
    <div class="card-head"><div>
      <div class="card-title">🧠 SEGUNDO CEREBRO · memoria completa por ticker</div>
      <div class="card-sub">Cómo le fue a ESTE sistema operando cada ticker. Alimenta el scorer (favorable +0.25, adverso -0.50).</div>
    </div></div>
    <table><thead><tr><th>Ticker</th><th>Trades</th><th>Win%</th><th>Avg P&L</th><th>Hold</th><th>Veredicto</th></tr></thead>
    <tbody>{rows}</tbody></table>
  </div>"""
    except Exception:
        pass

    return f"""
<div class="tab-content" id="tab-senales">
  <p class="ts" style="margin-bottom:16px">Ultima actualizacion: {gen_at} &nbsp;&middot;&nbsp; Clic en cada senal para ver el analisis completo &nbsp;&middot;&nbsp; ✓=ejecutada ⏳=pendiente/skip</p>
  {body}
  {mem_html}
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
        y:{{ticks:{{color:'#7d8590',callback:v=>parseFloat(v.toFixed(1))+'%',font:{{size:11}}}},grid:{{color:'#21262d'}}}}
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
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap');

*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}

:root{
  --bg:#08090d; --s1:#0f1117; --s2:#161a22; --bd:#252a35;
  --tx:#d1d5db; --mt:#6b7280; --ac:#f59e0b;
  --green:#22c55e; --red:#ef4444; --blue:#3b82f6; --purple:#8b5cf6;
  --mono:'IBM Plex Mono',monospace;
}

html{scroll-behavior:smooth}
body{font-family:'IBM Plex Sans',system-ui,sans-serif;background:var(--bg);color:var(--tx);
  padding:0 0 60px;max-width:1600px;margin:0 auto;line-height:1.5}

/* ── freshness banner ── */
.fresh-banner{padding:8px 28px;font-size:.78rem;font-weight:600;margin-bottom:0;letter-spacing:.2px}
.fresh-warn{background:#78350f18;color:#f59e0b;border-left:3px solid #f59e0b}
.fresh-stale{background:#7f1d1d22;color:#ef4444;border-left:3px solid #ef4444}

/* ── ticker tape (Bloomberg-style) ── */
.ticker-tape{
  background:#0f1117;border-bottom:1px solid var(--bd);
  padding:6px 28px;overflow:hidden;white-space:nowrap;
  font-family:var(--mono);font-size:.72rem;letter-spacing:.3px;
}
.tape-inner{display:inline-block;animation:tape 40s linear infinite}
.tape-inner:hover{animation-play-state:paused}
@keyframes tape{from{transform:translateX(100vw)}to{transform:translateX(-100%)}}
.tape-item{display:inline-block;margin-right:40px;color:var(--mt)}
.tape-item b{color:var(--tx)}
.tape-up{color:var(--green)}
.tape-dn{color:var(--red)}

/* ── progress bar ── */
.rbar{height:2px;background:linear-gradient(90deg,var(--ac),var(--blue));
  position:fixed;top:0;left:0;width:0%;transition:width 300s linear;z-index:100}

/* ── top bar ── */
.topbar{
  display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;
  padding:14px 28px 0;margin-bottom:16px;
  border-bottom:1px solid var(--bd);padding-bottom:14px;
}
.logo{font-family:var(--mono);font-size:1.1rem;font-weight:600;
  color:var(--ac);letter-spacing:1px}
.logo span{color:var(--mt);font-weight:400}
.topbar-meta{font-size:.72rem;color:var(--mt);display:flex;align-items:center;gap:12px;font-family:var(--mono)}
.live{display:inline-block;width:6px;height:6px;background:var(--green);
  border-radius:50%;animation:blink 2s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}

/* ── nav tabs ── */
.nav{
  display:flex;gap:0;padding:0 28px;
  border-bottom:1px solid var(--bd);margin-bottom:24px;
  overflow-x:auto;-webkit-overflow-scrolling:touch;background:var(--s1);
}
.tab-btn{
  padding:10px 20px;font-size:.78rem;font-weight:600;color:var(--mt);
  background:none;border:none;border-bottom:2px solid transparent;
  cursor:pointer;white-space:nowrap;transition:color .15s,border-color .15s,background .15s;
  margin-bottom:-1px;letter-spacing:.3px;text-transform:uppercase;font-family:var(--mono);
}
.tab-btn:hover{color:var(--tx);background:#ffffff08}
.tab-btn.active{color:var(--ac);border-bottom-color:var(--ac);background:#f59e0b08}

/* ── tab content ── */
.tab-content{display:none;padding:0 28px;animation:fadeIn .15s ease}
.tab-content.active{display:block}
@keyframes fadeIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}

/* ── KPI rows ── */
.kpi-row{
  display:grid;
  grid-template-columns:repeat(auto-fill,minmax(170px,1fr));
  gap:10px;margin-bottom:4px;
}
.kpi-row-adv{grid-template-columns:repeat(auto-fill,minmax(190px,1fr));margin-top:10px;margin-bottom:0}
.kpi{
  background:var(--s1);border:1px solid var(--bd);border-radius:4px;
  padding:14px 16px;transition:border-color .15s;
}
.kpi:hover{border-color:#f59e0b44}
.kpi-hero{background:var(--s1);border-color:#f59e0b33;border-left:3px solid var(--ac)}
.kpi-lbl{font-size:.62rem;color:var(--mt);text-transform:uppercase;
  letter-spacing:.9px;margin-bottom:8px;font-weight:600;font-family:var(--mono)}
.kpi-val{font-size:1.5rem;font-weight:600;line-height:1;margin-bottom:4px;font-family:var(--mono)}
.kpi-hero-val{font-size:1.8rem}
.kpi-sub{font-size:.7rem;color:var(--mt);font-family:var(--mono)}
.kpi-tag{font-size:.7rem;font-weight:600;padding:2px 8px;border-radius:2px;
  display:inline-block;margin-top:6px;font-family:var(--mono)}

/* ── cards ── */
.card{background:var(--s1);border:1px solid var(--bd);border-radius:4px;
  padding:18px 20px;margin-bottom:4px}
.card-head{display:flex;align-items:flex-start;justify-content:space-between;
  flex-wrap:wrap;gap:10px;margin-bottom:16px}
.card-title{font-size:.85rem;font-weight:700;color:var(--tx);margin-bottom:3px;
  text-transform:uppercase;letter-spacing:.5px;font-family:var(--mono)}
.card-sub{font-size:.74rem;color:var(--mt)}
.pill{font-size:.78rem;font-weight:600;background:var(--s2);
  border:1px solid var(--bd);border-radius:2px;padding:3px 10px;white-space:nowrap;font-family:var(--mono)}

/* ── two-col layout ── */
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:14px;align-items:start}
@media(max-width:900px){.two-col{grid-template-columns:1fr}}

/* ── section gap ── */
.section-gap{height:14px}

/* ── positions ── */
.pos-summary{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));
  gap:10px;margin-bottom:18px}
.pos-layout{display:flex;gap:20px;flex-wrap:wrap;align-items:flex-start}
.pos-chart-col{flex:0 0 210px;display:flex;flex-direction:column;align-items:center}
.pos-cards-col{flex:1;min-width:280px;display:grid;
  grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:10px}
.pos-card{background:var(--s2);border:1px solid var(--bd);border-radius:4px;
  padding:12px 14px;transition:border-color .15s}
.pos-card:hover{border-color:#f59e0b44}
.pos-top{display:flex;justify-content:space-between;margin-bottom:10px}
.pos-ticker{font-size:1rem;font-weight:700;font-family:var(--mono)}
.pos-sub{font-size:.68rem;color:var(--mt);margin-top:2px}
.pos-pnl{font-size:.88rem;font-weight:600;text-align:right;font-family:var(--mono)}
.pos-pnl-pct{font-size:.74rem;text-align:right;margin-top:2px;font-family:var(--mono)}
.pos-bar-track{height:3px;background:var(--bd);border-radius:1px;margin-bottom:10px}
.pos-meta{display:flex;gap:14px;flex-wrap:wrap;font-size:.72rem;color:var(--mt);font-family:var(--mono)}
.pos-meta b{color:var(--tx)}

/* ── alloc legend ── */
.alloc-legend{margin-top:12px;width:100%}
.leg-row{display:flex;align-items:center;gap:7px;font-size:.74rem;
  color:var(--tx);margin-bottom:5px;font-family:var(--mono)}
.leg-dot{width:6px;height:6px;border-radius:1px;flex-shrink:0}

/* ── signals ── */
.sleeve-block{margin-bottom:24px}
.sleeve-header{display:flex;align-items:center;gap:12px;
  padding:8px 12px;background:var(--s2);border-left:3px solid var(--ac);margin-bottom:12px}
.sleeve-label{font-size:.82rem;font-weight:700;font-family:var(--mono);text-transform:uppercase;letter-spacing:.5px}
.sleeve-count{font-size:.74rem;font-family:var(--mono)}
.sig-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:10px}
.sig-card{background:var(--s2);border:1px solid var(--bd);border-radius:4px;
  padding:12px 14px;cursor:pointer;transition:border-color .15s}
.sig-card:hover{border-color:#f59e0b55}
.sig-head{display:flex;justify-content:space-between;align-items:center;
  flex-wrap:wrap;gap:8px;margin-bottom:10px}
.sig-left{display:flex;align-items:center;gap:8px}
.sig-right{display:flex;align-items:center;gap:10px}
.sig-ticker{font-size:1rem;font-weight:700;font-family:var(--mono)}
.conv-badge{font-size:.66rem;font-weight:700;padding:2px 7px;border-radius:2px;font-family:var(--mono)}
.sig-alloc{font-size:.78rem;font-weight:600;color:var(--tx);font-family:var(--mono)}
.sig-arrow{font-size:.7rem;color:var(--mt);transition:transform .2s}
.sig-arrow.open{transform:rotate(180deg)}
.sig-pills{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
.spill{background:var(--s1);border:1px solid var(--bd);border-radius:3px;
  padding:5px 10px;min-width:80px;text-align:center}
.spill-l{font-size:.62rem;color:var(--mt);margin-bottom:2px;text-transform:uppercase;letter-spacing:.5px;font-family:var(--mono)}
.spill-v{font-size:.8rem;font-weight:600;font-family:var(--mono)}
.conv-track{height:2px;background:var(--bd);border-radius:1px}
.conv-fill{height:2px;border-radius:1px;opacity:.8}
.sig-thesis{display:none;margin-top:10px;padding:10px 12px;
  background:var(--bg);border-radius:3px;border-left:3px solid var(--ac);
  font-size:.8rem;color:#9ca3af;line-height:1.65;animation:fadeIn .2s ease}
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
.cal-cell{border-radius:8px;padding:10px 6px 8px;text-align:center;vertical-align:top;
  min-width:72px;height:86px;background:var(--s2);cursor:default;
  transition:transform .12s,box-shadow .12s}
.cal-cell:hover{transform:scale(1.06);box-shadow:0 4px 16px rgba(0,0,0,.5);
  z-index:2;position:relative}
.cal-empty{background:none}
.cal-wk{opacity:.35}
.cal-nd{opacity:.5}
.cal-n{display:block;font-size:.68rem;color:var(--mt);font-weight:600;margin-bottom:6px}
.cal-p{display:block;font-size:.88rem;font-weight:700;line-height:1.2}
.cal-q{display:block;font-size:.74rem;font-weight:600;margin-top:3px;opacity:.95}

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
.ts{font-size:.74rem;color:var(--mt);font-family:var(--mono)}
.muted{color:var(--mt)}

/* ── Monte Carlo panel ── */
.mc-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px;margin-top:12px}
.mc-cell{background:var(--bg);border:1px solid var(--bd);border-radius:3px;
  padding:10px 12px;text-align:center}
.mc-label{font-size:.6rem;color:var(--mt);text-transform:uppercase;letter-spacing:.7px;font-family:var(--mono);margin-bottom:5px}
.mc-val{font-size:1rem;font-weight:600;font-family:var(--mono)}

/* ── edgar alerts ── */
.edgar-alert{padding:8px 12px;border-left:3px solid;margin-bottom:6px;border-radius:0 3px 3px 0;font-size:.78rem}
.edgar-bull{border-color:var(--green);background:#22c55e0c}
.edgar-bear{border-color:var(--red);background:#ef44440c}
.edgar-neutral{border-color:var(--mt);background:#6b72800c}

@media(max-width:640px){
  .topbar,.nav,.tab-content{padding-left:14px;padding-right:14px}
  .ticker-tape{padding:6px 14px}
  .kpi-val{font-size:1.2rem}
  .kpi-hero-val{font-size:1.5rem}
  .sig-grid{grid-template-columns:1fr}
  .pos-chart-col{flex:0 0 100%}
}
"""


# ─── HTML completo ─────────────────────────────────────────────────────────────

def _tab_historial(trades: list[dict]) -> str:
    if not trades:
        return (
            '<div class="tab-content" id="tab-historial">'
            '<div class="card"><p class="muted" style="padding:40px;text-align:center">'
            'Sin historial de operaciones todavia. Las ordenes ejecutadas apareceran aqui.'
            '</p></div></div>'
        )

    # ── Calcular stats de trades cerrados ────────────────────────────────
    buys = [t for t in trades if t.get("side", "").upper() == "BUY"]
    closed = [t for t in buys if t.get("closed_at")]
    n_closed = len(closed)
    wins = [t for t in closed if (t.get("pnl_usd") or 0) > 0]
    losses = [t for t in closed if (t.get("pnl_usd") or 0) < 0]
    win_rate = len(wins) / n_closed * 100 if n_closed else None
    total_pnl = sum(t.get("pnl_usd") or 0 for t in closed)
    avg_hold = sum(t.get("hold_days") or 0 for t in closed) / n_closed if n_closed else None

    # iter15: calidad de salidas — avg win/loss, profit factor, expectancy.
    # Explica casos como "70% win pero P&L negativo" (losers > winners).
    gross_win = sum(t.get("pnl_usd") or 0 for t in wins)
    gross_loss = abs(sum(t.get("pnl_usd") or 0 for t in losses))
    avg_win = (gross_win / len(wins)) if wins else 0.0
    avg_loss = (gross_loss / len(losses)) if losses else 0.0
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    win_loss_ratio = (avg_win / avg_loss) if avg_loss > 0 else 0.0
    expectancy = (total_pnl / n_closed) if n_closed else 0.0

    # ── KPI bar ───────────────────────────────────────────────────────────
    def _wr_color(wr):
        return "#3fb950" if wr > 55 else "#d29922" if wr > 45 else "#f85149"

    kpi_html = ""
    if n_closed > 0:
        wr_c  = _wr_color(win_rate)
        pnl_c = _c(total_pnl)
        pnl_bg = _bg(total_pnl)
        kpi_html = f"""
  <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px">
    <div class="kpi-card" style="flex:1;min-width:110px">
      <div class="kpi-label">Trades cerrados</div>
      <div class="kpi-val">{n_closed}</div>
    </div>
    <div class="kpi-card" style="flex:1;min-width:110px">
      <div class="kpi-label">Win rate real</div>
      <div class="kpi-val" style="color:{wr_c}">{win_rate:.1f}%</div>
    </div>
    <div class="kpi-card" style="flex:1;min-width:130px;background:{pnl_bg}22">
      <div class="kpi-label">P&L realizado</div>
      <div class="kpi-val" style="color:{pnl_c}">{"+" if total_pnl>=0 else ""}{_usd(total_pnl)}</div>
    </div>
    <div class="kpi-card" style="flex:1;min-width:110px">
      <div class="kpi-label">Hold promedio</div>
      <div class="kpi-val">{avg_hold:.1f}d</div>
    </div>
  </div>
  <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px">
    <div class="kpi-card" style="flex:1;min-width:110px">
      <div class="kpi-label">Avg ganador</div>
      <div class="kpi-val" style="color:#3fb950">+{_usd(avg_win)}</div>
    </div>
    <div class="kpi-card" style="flex:1;min-width:110px">
      <div class="kpi-label">Avg perdedor</div>
      <div class="kpi-val" style="color:#f85149">-{_usd(avg_loss)}</div>
    </div>
    <div class="kpi-card" style="flex:1;min-width:110px">
      <div class="kpi-label">Win/Loss ratio</div>
      <div class="kpi-val" style="color:{'#3fb950' if win_loss_ratio>=1 else '#f85149'}">{win_loss_ratio:.2f}x</div>
    </div>
    <div class="kpi-card" style="flex:1;min-width:110px">
      <div class="kpi-label">Profit factor</div>
      <div class="kpi-val" style="color:{'#3fb950' if profit_factor>=1.3 else ('#d29922' if profit_factor>=1 else '#f85149')}">{('∞' if profit_factor==float('inf') else f'{profit_factor:.2f}')}</div>
    </div>
    <div class="kpi-card" style="flex:1;min-width:120px">
      <div class="kpi-label">Expectancy/trade</div>
      <div class="kpi-val" style="color:{_c(expectancy)}">{"+" if expectancy>=0 else ""}{_usd(expectancy)}</div>
    </div>
  </div>
  {('<div class="card" style="border-left:3px solid #d29922;font-size:.8rem;color:var(--mt);margin-bottom:14px">⚠️ Win rate alto pero Win/Loss < 1: cortás los ganadores temprano y dejás correr los perdedores. El trailing/stop necesita dejar correr más los winners.</div>' if (win_rate or 0) > 55 and win_loss_ratio < 1 and n_closed >= 5 else '')}"""

    # ── Desglose P&L por sleeve ───────────────────────────────────────────
    SLEEVE_META = {
        "LP":  ("Largo Plazo",  "#58a6ff"),
        "CP":  ("Corto Plazo",  "#d29922"),
        "OPT": ("Opciones",     "#bc8cff"),
        "MIX": ("LP+CP",        "#3fb950"),
    }
    sleeve_stats: dict[str, dict] = {}
    for t in closed:
        slv = (t.get("sleeve") or "—").upper()
        if slv not in sleeve_stats:
            sleeve_stats[slv] = {"n": 0, "wins": 0, "pnl": 0.0}
        sleeve_stats[slv]["n"] += 1
        if (t.get("pnl_usd") or 0) > 0:
            sleeve_stats[slv]["wins"] += 1
        sleeve_stats[slv]["pnl"] += t.get("pnl_usd") or 0

    sleeve_cards = ""
    for slv, st in sorted(sleeve_stats.items()):
        label, color = SLEEVE_META.get(slv, (slv, "#7d8590"))
        pnl = st["pnl"]
        wr  = st["wins"] / st["n"] * 100 if st["n"] else 0
        pc  = _c(pnl)
        wr_c = "#3fb950" if wr > 55 else "#d29922" if wr > 45 else "#f85149"
        sleeve_cards += f"""
<div style="flex:1;min-width:120px;padding:12px 14px;border:1px solid {color}33;
  border-left:3px solid {color};border-radius:8px;background:var(--bg)">
  <div style="font-size:.7rem;font-weight:700;color:{color};text-transform:uppercase;
    letter-spacing:.06em;margin-bottom:6px">{label}</div>
  <div style="font-size:1rem;font-weight:700;color:{pc};margin-bottom:2px">
    {"+" if pnl>=0 else ""}{_usd(pnl)}</div>
  <div style="font-size:.72rem;color:var(--mt)">
    WR <span style="color:{wr_c}">{wr:.0f}%</span> &nbsp;·&nbsp; {st["n"]} trades</div>
</div>"""

    if sleeve_cards and n_closed > 0:
        kpi_html += f"""
  <div style="margin-bottom:16px">
    <div style="font-size:.75rem;color:var(--mt);margin-bottom:8px;font-weight:600;
      text-transform:uppercase;letter-spacing:.05em">P&L por Sleeve</div>
    <div style="display:flex;gap:10px;flex-wrap:wrap">{sleeve_cards}</div>
  </div>"""

    # ── Filas de la tabla ─────────────────────────────────────────────────
    rows_html = ""
    for t in trades:
        side = t.get("side", "").upper()
        if side != "BUY":
            continue  # solo mostramos BUYs (con su PnL de cierre integrado)
        sleeve  = t.get("sleeve") or "—"
        notional = t.get("notional")
        entry   = t.get("price")
        exit_p  = t.get("exit_price")
        pnl_usd = t.get("pnl_usd")
        pnl_pct = t.get("pnl_pct")
        hold_d  = t.get("hold_days")
        is_closed = bool(t.get("closed_at"))

        # Estado visual
        if is_closed:
            status_label = "CERRADO"
            status_color = "#8b949e"
            row_style = ""
        else:
            status_label = "ABIERTO"
            status_color = "#3fb950"
            row_style = ""

        # PnL columns
        if is_closed and pnl_usd is not None:
            pc = _c(pnl_usd)
            pnl_cell = f'<td style="text-align:right;color:{pc};font-weight:600">{"+" if pnl_usd>=0 else ""}{_usd(pnl_usd)}</td>'
            pct_cell  = f'<td style="text-align:right;color:{pc}">{"+" if (pnl_pct or 0)>=0 else ""}{pnl_pct:.1f}%</td>'
        else:
            pnl_cell = '<td style="text-align:right;color:var(--mt)">—</td>'
            pct_cell = '<td style="text-align:right;color:var(--mt)">—</td>'

        exit_cell = f'<td style="text-align:right">${exit_p:,.2f}</td>' if exit_p else '<td style="text-align:right;color:var(--mt)">—</td>'
        hold_cell = f'<td style="text-align:right">{hold_d:.1f}d</td>' if hold_d else '<td style="text-align:right;color:var(--mt)">—</td>'

        rows_html += (
            f'<tr style="border-bottom:1px solid var(--border);{row_style}">'
            f'<td style="padding:7px 10px;color:var(--mt);font-size:.78rem">{_esc(t.get("date",""))}</td>'
            f'<td style="padding:7px 10px"><b>{_esc(t.get("ticker",""))}</b></td>'
            f'<td style="padding:7px 10px">{_esc(sleeve)}</td>'
            f'<td style="padding:7px 10px;text-align:right">{f"${entry:,.2f}" if entry else "—"}</td>'
            f'{exit_cell}'
            f'{pnl_cell}'
            f'{pct_cell}'
            f'{hold_cell}'
            f'<td style="padding:7px 10px;color:{status_color};font-size:.78rem">{status_label}</td>'
            f'</tr>'
        )

    return f"""<div class="tab-content" id="tab-historial">
<div class="card">
  <div class="card-head"><div>
    <div class="card-title">Historial de Operaciones</div>
    <div class="card-sub">{len(buys)} posiciones · {n_closed} cerradas</div>
  </div></div>
  {kpi_html}
  <div style="overflow-x:auto">
    <table style="width:100%;border-collapse:collapse;font-size:.82rem">
      <thead>
        <tr style="color:var(--mt);font-size:.78rem">
          <th style="text-align:left;padding:6px 10px">Fecha</th>
          <th style="text-align:left;padding:6px 10px">Ticker</th>
          <th style="text-align:left;padding:6px 10px">Sleeve</th>
          <th style="text-align:right;padding:6px 10px">Entrada</th>
          <th style="text-align:right;padding:6px 10px">Salida</th>
          <th style="text-align:right;padding:6px 10px">P&L $</th>
          <th style="text-align:right;padding:6px 10px">P&L %</th>
          <th style="text-align:right;padding:6px 10px">Hold</th>
          <th style="text-align:left;padding:6px 10px">Estado</th>
        </tr>
      </thead>
      <tbody>
        {rows_html}
      </tbody>
    </table>
  </div>
</div>
</div>"""


def _tab_daytrader(dt_trades: list[dict], dt_scan: dict | None = None) -> str:
    """Day Trading tab — sleeve DT, cuenta Alpaca separada."""
    buys        = [t for t in dt_trades if t.get("side", "").upper() == "BUY"]
    closed      = [t for t in buys if t.get("closed_at")]
    open_trades = [t for t in buys if not t.get("closed_at")]
    n_closed    = len(closed)
    wins        = [t for t in closed if (t.get("pnl_usd") or 0) > 0]
    win_rate    = len(wins) / n_closed * 100 if n_closed else None
    total_pnl   = sum(t.get("pnl_usd") or 0 for t in closed)
    best_t      = max(closed, key=lambda t: t.get("pnl_usd") or 0, default=None)
    worst_t     = min(closed, key=lambda t: t.get("pnl_usd") or 0, default=None)

    if not buys:
        scan_html = ""
        if dt_scan:
            scan_ts      = dt_scan.get("ts", "")[:16].replace("T", " ") + " UTC"
            scan_dir     = dt_scan.get("direction", "?")
            scan_found   = dt_scan.get("candidates_found", 0)
            scan_reason  = dt_scan.get("reason", "")
            scan_ticker  = dt_scan.get("best_ticker", "")
            scan_score   = dt_scan.get("best_score", 0.0)
            scan_traded  = dt_scan.get("traded", False)
            traded_label = (
                f'<span style="color:#3fb950;font-weight:700">OPERÓ {dt_scan.get("traded_ticker","")} '
                f'${dt_scan.get("notional",0):.0f}</span>'
            ) if scan_traded else '<span style="color:#f85149">Sin trade</span>'
            score_str    = f" | Mejor: {scan_ticker} score={scan_score:.3f}" if scan_ticker else ""
            scan_html = (
                f'<div style="margin-top:16px;padding:12px 16px;border:1px solid #30363d;'
                f'border-radius:6px;background:var(--bg);text-align:left;max-width:600px;margin-inline:auto">'
                f'<div style="font-size:.78rem;color:var(--mt);margin-bottom:4px">Último scan — {scan_ts}</div>'
                f'<div style="font-size:.85rem;margin-bottom:4px">'
                f'Mercado: <b style="color:#58a6ff">{scan_dir}</b> | '
                f'Candidatos: <b>{scan_found}</b>{score_str}</div>'
                f'<div style="font-size:.82rem">{traded_label}'
                f'<span style="color:var(--mt);margin-left:8px">{_esc(scan_reason[:180])}</span></div>'
                f'</div>'
            )
        return (
            '<div class="tab-content" id="tab-daytrader">'
            '<div class="card"><p class="muted" style="padding:40px 60px 24px;text-align:center;font-size:1rem">'
            'Sin operaciones DT todavia.<br>'
            '<span style="font-size:.82rem;color:var(--mt)">'
            'Cuenta separada: ALPACA_DT_API_KEY &mdash; estrategia gap+ORB+VWAP, 1 posicion de $1400<br>'
            'Corre via GitHub Actions a las <b>11:15 ART</b> (14:15 UTC), lun-vie'
            '</span>'
            f'{scan_html}'
            '</p></div></div>'
        )

    pnl_c   = _c(total_pnl)
    pnl_bg  = _bg(total_pnl)
    ret_pct = total_pnl / 1600 * 100

    kpi_html = f"""<div class="kpi-row" style="margin-bottom:16px">
  <div class="kpi kpi-hero">
    <div class="kpi-lbl">P&L Realizado DT</div>
    <div class="kpi-val" style="color:{pnl_c}">{"+" if total_pnl>=0 else ""}{_usd(total_pnl)}</div>
    <div class="kpi-tag" style="background:{pnl_bg};color:{pnl_c}">{"+" if ret_pct>=0 else ""}{ret_pct:.1f}% sobre $1600</div>
  </div>"""

    if win_rate is not None:
        wr_c = "#3fb950" if win_rate > 55 else "#d29922" if win_rate > 45 else "#f85149"
        kpi_html += f"""
  <div class="kpi">
    <div class="kpi-lbl">Win Rate DT</div>
    <div class="kpi-val" style="color:{wr_c}">{win_rate:.1f}%</div>
    <div class="kpi-sub">{len(wins)} de {n_closed} trades</div>
  </div>"""

    kpi_html += f"""
  <div class="kpi">
    <div class="kpi-lbl">Trades totales</div>
    <div class="kpi-val">{len(buys)}</div>
    <div class="kpi-sub">{n_closed} cerrados &middot; {len(open_trades)} abiertos</div>
  </div>"""

    if best_t:
        bpnl = best_t.get("pnl_usd") or 0
        kpi_html += f"""
  <div class="kpi">
    <div class="kpi-lbl">Mejor trade</div>
    <div class="kpi-val" style="color:#3fb950">+{_usd(bpnl)}</div>
    <div class="kpi-sub">{best_t.get("ticker","?")} {best_t.get("date","")[:10]}</div>
  </div>"""

    if worst_t and n_closed > 1:
        wpnl = worst_t.get("pnl_usd") or 0
        kpi_html += f"""
  <div class="kpi">
    <div class="kpi-lbl">Peor trade</div>
    <div class="kpi-val" style="color:#f85149">{_usd(wpnl)}</div>
    <div class="kpi-sub">{worst_t.get("ticker","?")} {worst_t.get("date","")[:10]}</div>
  </div>"""

    kpi_html += "</div>"

    # Cumulative P&L chart
    chart_html = ""
    if closed:
        sorted_closed = sorted(closed, key=lambda t: t.get("date", ""))
        dates    = [t.get("date", "")[:10] for t in sorted_closed]
        pnls_raw = [t.get("pnl_usd") or 0 for t in sorted_closed]
        cum_pnls = []
        running  = 0.0
        for p in pnls_raw:
            running += p
            cum_pnls.append(round(running, 2))
        last_cum    = cum_pnls[-1]
        chart_color = _c(last_cum)
        chart_html = f"""<div class="card" style="margin-bottom:14px">
  <div class="card-head">
    <div>
      <div class="card-title">P&L Acumulado &mdash; Day Trading</div>
      <div class="card-sub">Resultado intraday por operacion &middot; SL -1.5% &middot; TP1 +2.5% &middot; TP2 +5.0%</div>
    </div>
    <div class="pill" style="color:{chart_color}">{"+" if last_cum>=0 else ""}{_usd(last_cum)}</div>
  </div>
  <canvas id="dtChart" height="80"></canvas>
</div>
<script>
(function(){{
  const ctx=document.getElementById('dtChart');
  const g=ctx.getContext('2d').createLinearGradient(0,0,0,200);
  g.addColorStop(0,'{chart_color}33');g.addColorStop(1,'{chart_color}00');
  window._charts=window._charts||{{}};
  window._charts.dt=new Chart(ctx,{{
    type:'line',
    data:{{
      labels:{json.dumps(dates)},
      datasets:[{{data:{json.dumps(cum_pnls)},label:'P&L acumulado DT',
        borderColor:'{chart_color}',backgroundColor:g,fill:true,tension:.3,
        pointRadius:4,pointHoverRadius:7,pointBackgroundColor:'{chart_color}',borderWidth:2.5}}]
    }},
    options:{{
      animation:{{duration:900,easing:'easeInOutQuart'}},
      interaction:{{mode:'index',intersect:false}},
      plugins:{{
        legend:{{display:false}},
        tooltip:{{backgroundColor:'#161b22',borderColor:'#30363d',borderWidth:1,
          titleColor:'#7d8590',bodyColor:'#e6edf3',padding:12,
          callbacks:{{label:c=>` P&L: ${{c.parsed.y>=0?'+':''}}$${{Math.abs(c.parsed.y).toFixed(2)}}`}}
        }}
      }},
      scales:{{
        x:{{ticks:{{color:'#7d8590',font:{{size:11}}}},grid:{{color:'#21262d'}}}},
        y:{{ticks:{{color:'#7d8590',callback:v=>(v>=0?'+':'')+v.toFixed(0)+'$',font:{{size:11}}}},grid:{{color:'#21262d'}}}}
      }}
    }}
  }});
}})();
</script>"""

    # Trade table
    rows_html = ""
    for t in buys:
        date_str  = t.get("date","")[:10]
        ticker    = t.get("ticker","")
        entry     = t.get("price")
        exit_p    = t.get("exit_price")
        sl        = t.get("stop_loss")
        tp        = t.get("take_profit")
        pnl_usd   = t.get("pnl_usd")
        pnl_pct   = t.get("pnl_pct")
        is_closed = bool(t.get("closed_at"))
        status_label = "CERRADO" if is_closed else "ABIERTO"
        status_color = "#8b949e" if is_closed else "#3fb950"

        if is_closed and pnl_usd is not None:
            pc       = _c(pnl_usd)
            pnl_cell = f'<td style="text-align:right;color:{pc};font-weight:700">{"+" if pnl_usd>=0 else ""}{_usd(pnl_usd)}</td>'
            pct_cell = f'<td style="text-align:right;color:{pc}">{"+" if (pnl_pct or 0)>=0 else ""}{(pnl_pct or 0):.1f}%</td>'
        else:
            pnl_cell = '<td style="text-align:right;color:var(--mt)">—</td>'
            pct_cell = '<td style="text-align:right;color:var(--mt)">—</td>'

        exit_cell = f'<td style="text-align:right">${exit_p:,.2f}</td>' if exit_p else '<td style="text-align:right;color:var(--mt)">—</td>'
        sl_cell   = f'<td style="text-align:right;color:#f85149">${sl:,.2f}</td>' if sl else '<td style="text-align:right;color:var(--mt)">—</td>'
        tp_cell   = f'<td style="text-align:right;color:#3fb950">${tp:,.2f}</td>' if tp else '<td style="text-align:right;color:var(--mt)">—</td>'

        rows_html += (
            f"<tr>"
            f'<td style="color:var(--mt);font-size:.78rem">{date_str}</td>'
            f'<td><b style="color:#f59e0b">{_esc(ticker)}</b></td>'
            f'<td style="text-align:right">{f"${entry:,.2f}" if entry else "—"}</td>'
            f'{sl_cell}{tp_cell}{exit_cell}{pnl_cell}{pct_cell}'
            f'<td style="color:{status_color};font-size:.78rem">{status_label}</td>'
            f"</tr>"
        )

    table_html = f"""<div class="card">
  <div class="card-head">
    <div>
      <div class="card-title">Log de Operaciones DT</div>
      <div class="card-sub">Cuenta separada &middot; $1400/trade &middot; gap+ORB+VWAP+RSI &middot; Dual bracket</div>
    </div>
  </div>
  <div style="overflow-x:auto">
    <table style="width:100%;border-collapse:collapse;font-size:.82rem">
      <thead>
        <tr style="color:var(--mt);font-size:.78rem">
          <th style="text-align:left;padding:6px 10px">Fecha</th>
          <th style="text-align:left;padding:6px 10px">Ticker</th>
          <th style="text-align:right;padding:6px 10px">Entrada</th>
          <th style="text-align:right;padding:6px 10px">SL</th>
          <th style="text-align:right;padding:6px 10px">TP</th>
          <th style="text-align:right;padding:6px 10px">Salida</th>
          <th style="text-align:right;padding:6px 10px">P&L $</th>
          <th style="text-align:right;padding:6px 10px">P&L %</th>
          <th style="text-align:left;padding:6px 10px">Estado</th>
        </tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>
</div>"""

    info_html = """<div class="card" style="border-left:3px solid #f59e0b">
  <div class="card-head">
    <div>
      <div class="card-title">Estrategia Day Trading</div>
      <div class="card-sub">Cuenta Alpaca separada &middot; ALPACA_DT_API_KEY</div>
    </div>
  </div>
  <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px;font-size:.8rem;color:var(--mt)">
    <div><b style="color:var(--tx)">Capital DT</b><br>$1400/trade (87.5% de $1600)</div>
    <div><b style="color:var(--tx)">Posiciones</b><br>1 concentrada por dia</div>
    <div><b style="color:var(--tx)">Stop Loss</b><br>-1.5% fijo</div>
    <div><b style="color:var(--tx)">Dual bracket</b><br>TP1 +3% (50%) &middot; TP2 +7% (50%)</div>
    <div><b style="color:var(--tx)">Filtros</b><br>Gap &gt;1% &middot; VWAP &middot; ORB &middot; Vol 1.2x &middot; RSI 38-78</div>
    <div><b style="color:var(--tx)">Cierre EOD</b><br>15:00 EDT automatico</div>
    <div><b style="color:var(--tx)">Horario entrada</b><br>11:30 ART (10:30 EDT)</div>
    <div><b style="color:var(--tx)">Swarm IA</b><br>4 agentes CoT + meta-agente EV</div>
  </div>
</div>"""

    # ── Swarm debate log ──────────────────────────────────────────────────────
    swarm_html = ""
    try:
        from alpha_agent.swarm.orchestrator import load_debates
        debates = load_debates(limit=5)
        if debates:
            agent_icons = {
                "Strategist":  ("S", "#818cf8"),
                "Technical":   ("T", "#34d399"),
                "Sentiment":   ("N", "#fbbf24"),
                "RiskAuditor": ("R", "#f87171"),
            }
            stance_colors = {"GO": "#3fb950", "NO-GO": "#f85149", "REDUCE": "#d29922"}

            debate_cards = ""
            for deb in debates:
                ticker_d  = deb.get("ticker", "?")
                dir_d     = deb.get("direction", "LONG")
                ts_d      = deb.get("ts", "")[:16].replace("T", " ")
                go_cnt    = deb.get("go_count", 0)
                meta      = deb.get("decision", {})
                ev_d      = deb.get("ev_data", {})
                ev_val    = ev_d.get("ev", 0)
                go_final  = meta.get("go", False)
                size_f    = meta.get("size_factor", 1.0)
                reason_d  = _esc(meta.get("reasoning", "")[:180])

                dir_color = "#3fb950" if dir_d == "LONG" else "#f85149"
                fin_color = "#3fb950" if go_final else "#f85149"
                ev_color  = "#3fb950" if ev_val >= 0 else "#f85149"
                ev_sign   = "+" if ev_val >= 0 else ""

                opinion_rows = ""
                for op in deb.get("opinions", []):
                    ag      = op.get("agent", "?")
                    stance  = op.get("stance", "?")
                    conf    = op.get("confidence", 0)
                    reas    = _esc(op.get("reasoning", "")[:140])
                    cot     = op.get("chain_of_thought", "").strip()
                    icon, ic = agent_icons.get(ag, ("?", "#8b949e"))
                    sc      = stance_colors.get(stance, "#8b949e")

                    cot_lines_html = ""
                    if cot:
                        for ln in cot.splitlines():
                            if ln.strip():
                                cot_lines_html += (
                                    f'<div style="color:var(--mt);font-size:.74rem;'
                                    f'padding:2px 0 2px 8px;border-left:2px solid {ic}33">'
                                    f'{_esc(ln.strip())}</div>'
                                )

                    ev_badge = ""
                    if ag == "RiskAuditor" and ev_d:
                        ev_badge = (
                            f'<span style="margin-left:8px;font-size:.72rem;'
                            f'background:{ev_color}22;color:{ev_color};'
                            f'padding:1px 6px;border-radius:4px">'
                            f'EV {ev_sign}${abs(ev_val):.1f}</span>'
                        )

                    opinion_rows += f"""
<div style="margin-bottom:10px">
  <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
    <div style="width:22px;height:22px;border-radius:50%;background:{ic}22;
      color:{ic};font-weight:700;font-size:.72rem;display:flex;
      align-items:center;justify-content:center;flex-shrink:0">{icon}</div>
    <span style="font-size:.78rem;font-weight:600;color:var(--tx)">{ag}</span>
    <span style="font-size:.72rem;font-weight:700;color:{sc};
      background:{sc}22;padding:1px 7px;border-radius:4px">{stance}</span>
    <span style="font-size:.72rem;color:var(--mt)">{conf}%</span>
    {ev_badge}
  </div>
  {cot_lines_html}
  <div style="font-size:.76rem;color:var(--mt);padding-left:30px;margin-top:3px">{reas}</div>
</div>"""

                debate_cards += f"""<div style="border:1px solid var(--br);border-radius:8px;
  padding:14px;margin-bottom:14px;background:var(--bg)">
  <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;flex-wrap:wrap">
    <span style="font-weight:700;color:var(--tx);font-size:.9rem">{_esc(ticker_d)}</span>
    <span style="font-size:.75rem;color:{dir_color};background:{dir_color}22;
      padding:1px 8px;border-radius:4px">{dir_d}</span>
    <span style="font-size:.72rem;color:var(--mt)">{ts_d}</span>
    <span style="font-size:.75rem;color:#8b949e">GO {go_cnt}/4</span>
    <span style="font-size:.75rem;color:{ev_color};background:{ev_color}22;
      padding:1px 8px;border-radius:4px">EV {ev_sign}${ev_val:.1f}</span>
    <span style="margin-left:auto;font-size:.8rem;font-weight:700;color:{fin_color}">
      {'GO' if go_final else 'NO-GO'} &times;{size_f}</span>
  </div>
  {opinion_rows}
  <div style="margin-top:8px;padding:8px 12px;background:{fin_color}11;
    border-radius:6px;font-size:.76rem;color:var(--mt);border-left:3px solid {fin_color}">
    <b style="color:{fin_color}">Meta-agente:</b> {reason_d}
  </div>
</div>"""

            swarm_html = f"""<div class="card" style="border-left:3px solid #818cf8">
  <div class="card-head" style="margin-bottom:14px">
    <div>
      <div class="card-title">Debate del Swarm &mdash; Ultimas {len(debates)} decisiones</div>
      <div class="card-sub">
        S=Strategist &middot; T=Technical &middot; N=Sentiment &middot; R=RiskAuditor
        &middot; CoT = cadena de pensamiento &middot; EV = Expected Value
      </div>
    </div>
  </div>
  {debate_cards}
</div>"""
    except Exception as e_deb:
        swarm_html = f'<div class="card"><p class="muted">Debate log no disponible: {_esc(str(e_deb)[:80])}</p></div>'

    return f"""<div class="tab-content" id="tab-daytrader">
  {kpi_html}
  {chart_html}
  <div class="section-gap"></div>
  {table_html}
  <div class="section-gap"></div>
  {swarm_html}
  <div class="section-gap"></div>
  {info_html}
</div>"""


# ─── TAB: SCALPING ────────────────────────────────────────────────────────────

def _tab_scalper(scalp_trades: list[dict]) -> str:
    """Scalping tab — sleeve SCALP, cuenta Alpaca separada (ALPACA_SCALP_*)."""
    buys        = [t for t in scalp_trades if t.get("side", "").upper() in ("BUY", "SELL")]
    closed      = [t for t in buys if t.get("closed_at")]
    open_trades = [t for t in buys if not t.get("closed_at")]
    n_closed    = len(closed)
    wins        = [t for t in closed if (t.get("pnl_usd") or 0) > 0]
    win_rate    = len(wins) / n_closed * 100 if n_closed else None
    total_pnl   = sum(t.get("pnl_usd") or 0 for t in closed)
    best_t      = max(closed, key=lambda t: t.get("pnl_usd") or 0, default=None)
    worst_t     = min(closed, key=lambda t: t.get("pnl_usd") or 0, default=None)

    if not buys:
        return (
            '<div class="tab-content" id="tab-scalper">'
            '<div class="card"><p class="muted" style="padding:60px;text-align:center;font-size:1rem">'
            'Sin operaciones SCALP todavia. El Scalper corre manualmente durante horario de mercado (9:30-15:45 EDT).<br>'
            '<span style="font-size:.82rem;color:var(--mt)">'
            'Cuenta separada: ALPACA_SCALP_API_KEY &mdash; estrategia ORB 15-min, $400/trade, max 4 trades/dia'
            '</span></p></div></div>'
        )

    pnl_c     = _c(total_pnl)
    pnl_bg    = _bg(total_pnl)
    ret_pct   = total_pnl / 1600 * 100

    kpi_html = f"""<div class="kpi-row" style="margin-bottom:16px">
  <div class="kpi kpi-hero">
    <div class="kpi-lbl">P&L Realizado SCALP</div>
    <div class="kpi-val" style="color:{pnl_c}">{"+" if total_pnl>=0 else ""}{_usd(total_pnl)}</div>
    <div class="kpi-tag" style="background:{pnl_bg};color:{pnl_c}">{"+" if ret_pct>=0 else ""}{ret_pct:.1f}% sobre $1600</div>
  </div>"""

    if win_rate is not None:
        wr_c = "#3fb950" if win_rate > 55 else "#d29922" if win_rate > 45 else "#f85149"
        kpi_html += f"""
  <div class="kpi">
    <div class="kpi-lbl">Win Rate</div>
    <div class="kpi-val" style="color:{wr_c}">{win_rate:.1f}%</div>
    <div class="kpi-sub">{len(wins)} de {n_closed} trades</div>
  </div>"""

    kpi_html += f"""
  <div class="kpi">
    <div class="kpi-lbl">Trades totales</div>
    <div class="kpi-val">{len(buys)}</div>
    <div class="kpi-sub">{n_closed} cerrados &middot; {len(open_trades)} abiertos</div>
  </div>"""

    if best_t:
        bpnl = best_t.get("pnl_usd") or 0
        kpi_html += f"""
  <div class="kpi">
    <div class="kpi-lbl">Mejor scalp</div>
    <div class="kpi-val" style="color:#3fb950">+{_usd(bpnl)}</div>
    <div class="kpi-sub">{best_t.get("ticker","?")} {best_t.get("date","")[:10]}</div>
  </div>"""

    if worst_t and n_closed > 1:
        wpnl = worst_t.get("pnl_usd") or 0
        kpi_html += f"""
  <div class="kpi">
    <div class="kpi-lbl">Peor scalp</div>
    <div class="kpi-val" style="color:#f85149">{_usd(wpnl)}</div>
    <div class="kpi-sub">{worst_t.get("ticker","?")} {worst_t.get("date","")[:10]}</div>
  </div>"""

    kpi_html += "</div>"

    # Cumulative P&L chart
    chart_html = ""
    if closed:
        sorted_closed = sorted(closed, key=lambda t: t.get("date",""))
        dates    = [t.get("date","")[:10] for t in sorted_closed]
        pnls_raw = [t.get("pnl_usd") or 0 for t in sorted_closed]
        cum_pnls, running = [], 0.0
        for p in pnls_raw:
            running += p
            cum_pnls.append(round(running, 2))
        last_cum    = cum_pnls[-1]
        chart_color = _c(last_cum)
        import json as _json
        dates_js = _json.dumps(dates)
        pnls_js  = _json.dumps(cum_pnls)
        chart_html = f"""<div class="card" style="margin-bottom:14px">
  <div class="card-head">
    <div>
      <div class="card-title">P&L Acumulado &mdash; Scalping</div>
      <div class="card-sub">ORB 15-min &middot; SL 0.3-0.5% &middot; TP 0.4-1.5% &middot; $400/trade</div>
    </div>
    <div class="pill" style="color:{chart_color}">{"+" if last_cum>=0 else ""}{_usd(last_cum)}</div>
  </div>
  <canvas id="scalpChart" height="80"></canvas>
</div>
<script>
(function(){{
  const ctx=document.getElementById('scalpChart');
  const g=ctx.getContext('2d').createLinearGradient(0,0,0,200);
  g.addColorStop(0,'{chart_color}33');g.addColorStop(1,'{chart_color}00');
  window._charts=window._charts||{{}};
  window._charts.scalp=new Chart(ctx,{{
    type:'line',
    data:{{
      labels:{dates_js},
      datasets:[{{data:{pnls_js},borderColor:'{chart_color}',backgroundColor:g,
        borderWidth:2,pointRadius:3,tension:0.3,fill:true}}]
    }},
    options:{{
      responsive:true,plugins:{{legend:{{display:false}},tooltip:{{callbacks:{{
        label:ctx=>'$'+ctx.raw.toFixed(2)
      }}}}}},
      scales:{{x:{{ticks:{{color:'#8b949e',maxTicksLimit:8}},grid:{{color:'#30363d22'}}}},
               y:{{ticks:{{color:'#8b949e',callback:v=>'$'+v}},grid:{{color:'#30363d44'}}}}}}
    }}
  }});
}})();
</script>"""

    # Trade table
    rows_html = ""
    for t in sorted(buys, key=lambda x: x.get("date",""), reverse=True):
        date_str = t.get("date","")[:10]
        ticker   = t.get("ticker","")
        side     = t.get("side","BUY").upper()
        entry    = t.get("price")
        exit_p   = t.get("exit_price")
        sl       = t.get("stop_loss")
        tp       = t.get("take_profit")
        pnl_usd  = t.get("pnl_usd")
        pnl_pct  = t.get("pnl_pct")
        is_closed = bool(t.get("closed_at"))
        side_color = "#3fb950" if side == "BUY" else "#f85149"
        status_label = "CERRADO" if is_closed else "ABIERTO"
        status_color = "#8b949e" if is_closed else "#3fb950"
        if is_closed and pnl_usd is not None:
            pc       = _c(pnl_usd)
            pnl_cell = f'<td style="text-align:right;color:{pc};font-weight:700">{"+" if pnl_usd>=0 else ""}{_usd(pnl_usd)}</td>'
            pct_cell = f'<td style="text-align:right;color:{pc}">{"+" if (pnl_pct or 0)>=0 else ""}{(pnl_pct or 0):.2f}%</td>'
        else:
            pnl_cell = '<td style="text-align:right;color:var(--mt)">—</td>'
            pct_cell = '<td style="text-align:right;color:var(--mt)">—</td>'
        exit_cell = f'<td style="text-align:right">${exit_p:,.2f}</td>' if exit_p else '<td style="text-align:right;color:var(--mt)">—</td>'
        sl_cell   = f'<td style="text-align:right;color:#f85149">${sl:,.2f}</td>' if sl else '<td>—</td>'
        tp_cell   = f'<td style="text-align:right;color:#3fb950">${tp:,.2f}</td>' if tp else '<td>—</td>'
        rows_html += (
            f"<tr>"
            f'<td style="color:var(--mt);font-size:.78rem">{date_str}</td>'
            f'<td><b style="color:#f59e0b">{_esc(ticker)}</b></td>'
            f'<td style="color:{side_color};font-size:.78rem;font-weight:700">{side}</td>'
            f'<td style="text-align:right">{f"${entry:,.2f}" if entry else "—"}</td>'
            f'{sl_cell}{tp_cell}{exit_cell}{pnl_cell}{pct_cell}'
            f'<td style="color:{status_color};font-size:.78rem">{status_label}</td>'
            f"</tr>"
        )

    table_html = f"""<div class="card">
  <div class="card-head">
    <div>
      <div class="card-title">Log de Operaciones SCALP</div>
      <div class="card-sub">Cuenta separada &middot; $400/trade &middot; ORB 15-min &middot; Swarm 2-agentes</div>
    </div>
  </div>
  <div style="overflow-x:auto">
    <table style="width:100%;border-collapse:collapse;font-size:.82rem">
      <thead>
        <tr style="color:var(--mt);font-size:.78rem">
          <th style="text-align:left;padding:6px 10px">Fecha</th>
          <th style="text-align:left;padding:6px 10px">Ticker</th>
          <th style="text-align:left;padding:6px 10px">Lado</th>
          <th style="text-align:right;padding:6px 10px">Entrada</th>
          <th style="text-align:right;padding:6px 10px">SL</th>
          <th style="text-align:right;padding:6px 10px">TP</th>
          <th style="text-align:right;padding:6px 10px">Salida</th>
          <th style="text-align:right;padding:6px 10px">P&L $</th>
          <th style="text-align:right;padding:6px 10px">P&L %</th>
          <th style="text-align:left;padding:6px 10px">Estado</th>
        </tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>
</div>"""

    info_html = """<div class="card" style="border-left:3px solid #34d399">
  <div class="card-head">
    <div>
      <div class="card-title">Estrategia Scalping ORB</div>
      <div class="card-sub">Cuenta Alpaca separada &middot; ALPACA_SCALP_API_KEY</div>
    </div>
  </div>
  <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px;font-size:.8rem;color:var(--mt)">
    <div><b style="color:var(--tx)">Capital</b><br>$400/trade (cuenta separada $1600)</div>
    <div><b style="color:var(--tx)">Max trades/dia</b><br>4 trades &middot; 1 por ticker</div>
    <div><b style="color:var(--tx)">Rango ORB</b><br>9:30-9:45 ET (15 min)</div>
    <div><b style="color:var(--tx)">Stop Loss</b><br>0.3-0.5% (borde opuesto del rango)</div>
    <div><b style="color:var(--tx)">Take Profit</b><br>0.4-1.5% (1.5x el rango)</div>
    <div><b style="color:var(--tx)">R/R</b><br>~2:1 (TP 1.5x el SL)</div>
    <div><b style="color:var(--tx)">Cierre EOD</b><br>15:45 ET forzado</div>
    <div><b style="color:var(--tx)">Swarm</b><br>2 agentes Haiku &lt;3s latencia</div>
    <div><b style="color:var(--tx)">Ejecucion</b><br>Proceso local (no GitHub Actions)</div>
    <div><b style="color:var(--tx)">Universe</b><br>13 tickers alta liquidez</div>
  </div>
</div>"""

    return f"""<div class="tab-content" id="tab-scalper">
  {kpi_html}
  {chart_html}
  <div class="section-gap"></div>
  {table_html}
  <div class="section-gap"></div>
  {info_html}
</div>"""


def build_html(equity, initial, history, positions, signals_data,
               spy_history=None, qqq_history=None, metrics=None, perf_data=None, discovery_data=None,
               trades=None, mc_result=None, dt_trades=None, scalp_trades=None,
               workflow_status=None, dt_scan=None, brk_history=None):
    if spy_history is None:
        spy_history = []
    if qqq_history is None:
        qqq_history = []
    if brk_history is None:
        brk_history = []
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

    tres_cuentas   = _tres_cuentas_panel(trades or [], dt_trades or [], scalp_trades or [])
    wf_health      = _workflow_health_panel(workflow_status or {})
    llm_status     = _llm_status_panel()
    # Iter7+10: 4 cards nuevas
    risk_band      = _risk_band_panel(equity, initial)
    regime_active  = _regime_active_panel(signals_data)
    orphan_html    = _orphan_positions_panel(positions or [], signals_data)
    control_center = _control_center_panel()  # iter10/11
    adv_metrics    = _deployment_panel(equity, positions, signals_data)  # iter15: despliegue
    adv_metrics   += _advanced_metrics_panel(history, qqq_history, spy_history, brk_history)  # iter11/12
    adv_metrics   += _learnings_panel()  # iter13: segundo cerebro
    t_resumen    = _tab_resumen(equity, initial, regime, vix, wti, gold, dxy,
                                history, spy_history, signals_data, metrics, age_hours,
                                perf_data=perf_data, qqq_history=qqq_history, mc_result=mc_result,
                                tres_cuentas_html=tres_cuentas, wf_health_html=wf_health,
                                llm_status_html=llm_status,
                                risk_band_html=risk_band, regime_active_html=regime_active,
                                orphan_html=orphan_html,
                                control_center_html=control_center,
                                adv_metrics_html=adv_metrics)
    t_posiciones = _tab_posiciones(positions, signals_data)
    t_senales    = _tab_senales(signals_data, positions=positions)
    t_mercado    = _tab_mercado(signals_data, discovery_data=discovery_data)
    t_historial  = _tab_historial(trades or [])
    t_daytrader  = _tab_daytrader(dt_trades or [], dt_scan=dt_scan)
    t_scalper    = _tab_scalper(scalp_trades or [])

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
  <div class="logo">ALPHA<span>&nbsp;/&nbsp;TERMINAL</span></div>
  <div class="topbar-meta">
    <span><span class="live"></span>&nbsp;{now}</span>
    <span style="color:var(--bd)">|</span>
    <span>Paper</span>
    <span style="color:var(--bd)">|</span>
    <span id="countdown"></span>
  </div>
</div>

<div class="ticker-tape">
  <div class="tape-inner" id="tape-inner">
    <span class="tape-item">VIX <b id="t-vix">{vix:.1f}</b></span>
    <span class="tape-item">WTI <b id="t-wti">${wti:.1f}</b></span>
    <span class="tape-item">GOLD <b id="t-gold">${gold:.0f}</b></span>
    <span class="tape-item">DXY <b id="t-dxy">{dxy:.1f}</b></span>
    <span class="tape-item">RÉGIMEN <b style="color:{'#22c55e' if regime=='bull' else '#ef4444' if regime=='bear' else '#f59e0b'}">{regime.upper()}</b></span>
    <span class="tape-item">·&nbsp;&nbsp;&nbsp;·&nbsp;&nbsp;&nbsp;·</span>
    <span class="tape-item">VIX <b id="t-vix2">{vix:.1f}</b></span>
    <span class="tape-item">WTI <b id="t-wti2">${wti:.1f}</b></span>
    <span class="tape-item">GOLD <b id="t-gold2">${gold:.0f}</b></span>
    <span class="tape-item">DXY <b id="t-dxy2">{dxy:.1f}</b></span>
    <span class="tape-item">RÉGIMEN <b style="color:{'#22c55e' if regime=='bull' else '#ef4444' if regime=='bear' else '#f59e0b'}">{regime.upper()}</b></span>
  </div>
</div>

<nav class="nav">
  <button class="tab-btn active" data-tab="resumen">Resumen</button>
  <button class="tab-btn" data-tab="posiciones">Posiciones</button>
  <button class="tab-btn" data-tab="senales">Senales</button>
  <button class="tab-btn" data-tab="mercado">Mercado</button>
  <button class="tab-btn" data-tab="historial">Historial LP/CP</button>
  <button class="tab-btn" data-tab="daytrader">Day Trading</button>
  <button class="tab-btn" data-tab="scalper">Scalping</button>
</nav>

{t_resumen}
{t_posiciones}
{t_senales}
{t_mercado}
{t_historial}
{t_daytrader}
{t_scalper}

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
        alpaca_equity = broker.get_equity()
        positions     = broker.get_positions()
        # Convertir equity de Alpaca ($100k paper) al equivalente virtual ($1600 + P&L)
        try:
            from alpha_agent.analytics.capital_tracker import get_virtual_equity, get_initial_capital
            equity  = get_virtual_equity(alpaca_equity)
            initial = get_initial_capital()
        except Exception:
            equity  = alpaca_equity
            initial = PARAMS.paper_capital_usd
    except Exception as e:
        logger.error("Error Alpaca: %s", e)
        equity, positions = PARAMS.paper_capital_usd, []
        initial = PARAMS.paper_capital_usd

    # Portfolio history (1M) — escalado al equity virtual ($1600 base)
    history: list[dict] = []
    try:
        from alpaca.trading.requests import GetPortfolioHistoryRequest
        req = GetPortfolioHistoryRequest(period="1M", timeframe="1D")
        ph  = broker._trading.get_portfolio_history(req)
        if ph and ph.equity:
            raw_hist = [
                {"ts": int(t), "equity": float(e)}
                for t, e in zip(ph.timestamp or [], ph.equity)
                if e is not None and float(e) > 0
            ]
            # Escalar el historial al espacio virtual ($1600 base)
            # Los retornos % son idénticos; solo escalamos los valores absolutos
            if raw_hist and alpaca_equity > 0:
                scale = equity / alpaca_equity  # ≈ 1600/101586 ≈ 0.01575
                history = [{"ts": r["ts"], "equity": round(r["equity"] * scale, 2)} for r in raw_hist]
            else:
                history = raw_hist
        logger.info("Portfolio history: %d entradas (escalado virtual)", len(history))
    except Exception as e:
        logger.warning("Portfolio history no disponible: %s", e)

    # Fallback: equity snapshots guardados por el monitor (si Alpaca falla)
    if not history:
        try:
            _snap_path = BASE_DIR / "signals" / "equity_snapshots.json"
            if _snap_path.exists():
                import time as _time
                from datetime import date as _date
                _raw = json.loads(_snap_path.read_text(encoding="utf-8"))
                _valid: list[dict] = []
                for s in (_raw if isinstance(_raw, list) else []):
                    try:
                        _d = _date.fromisoformat(s["date"])    # valida formato fecha
                        _eq = float(s["equity"])
                        if _eq > 0:
                            _valid.append({"ts": int(_time.mktime(_d.timetuple())), "equity": _eq})
                    except Exception:
                        continue   # skip entradas corruptas, no abortar
                if _valid:
                    history = sorted(_valid, key=lambda x: x["ts"])
                    logger.info("Portfolio history (desde snapshots): %d entradas válidas de %d totales",
                                len(history), len(_raw))
                elif _raw:
                    logger.warning("equity_snapshots.json: %d entradas, ninguna válida", len(_raw))
        except Exception as _ef:
            logger.warning("equity_snapshots fallback error: %s", _ef)

    # SPY + QQQ + BRK-B (Buffett) benchmarks — cache de 1h para no re-descargar
    spy_history: list[dict] = []
    qqq_history: list[dict] = []
    brk_history: list[dict] = []  # iter12: Berkshire = proxy de Warren Buffett
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

    def _save_bench_cache(spy: list, qqq: list, brk: list) -> None:
        try:
            bench_cache_path.write_text(
                json.dumps({"ts": datetime.now().timestamp(), "spy": spy, "qqq": qqq, "brk": brk}),
                encoding="utf-8",
            )
        except Exception:
            pass

    if len(history) >= 2:
        cached = _load_bench_cache()
        if cached.get("spy") and cached.get("qqq") and cached.get("brk"):
            spy_history = cached["spy"]
            qqq_history = cached["qqq"]
            brk_history = cached["brk"]
            logger.info("Benchmarks cargados desde cache (%d SPY, %d QQQ, %d BRK)",
                        len(spy_history), len(qqq_history), len(brk_history))
        else:
            try:
                import yfinance as yf
                bench_df = yf.download("SPY QQQ BRK-B", period="1mo", progress=False, auto_adjust=True)
                if not bench_df.empty:
                    close = bench_df["Close"]
                    if hasattr(close, "squeeze") and close.ndim == 1:
                        close = close.to_frame()
                    for ticker, hist_list in [("SPY", spy_history), ("QQQ", qqq_history), ("BRK-B", brk_history)]:
                        if ticker in close.columns:
                            col = close[ticker].dropna()
                            if len(col) >= 2:
                                base = float(col.iloc[0])
                                for ts, v in zip(col.index, col.values):
                                    hist_list.append({"ts": int(ts.timestamp()), "equity": float(v) / base})
                    # Normalizar las listas (equity = ratio, no $ absolutos)
                    logger.info("Benchmarks descargados: %d SPY, %d QQQ, %d BRK",
                                len(spy_history), len(qqq_history), len(brk_history))
                    _save_bench_cache(spy_history, qqq_history, brk_history)
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

    # Workflow status (GitHub Actions health)
    workflow_status: dict = {}
    ws_path = BASE_DIR / "signals" / "workflow_status.json"
    if ws_path.exists():
        try:
            workflow_status = json.loads(ws_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # DayTrader last scan (escrito por run_daytrader.py aunque no opere)
    dt_scan_data: dict | None = None
    dt_scan_path = BASE_DIR / "signals" / "dt_last_scan.json"
    if dt_scan_path.exists():
        try:
            dt_scan_data = json.loads(dt_scan_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Trade history from SQLite — separado por cuenta
    trades: list[dict]       = []
    dt_trades: list[dict]    = []
    scalp_trades: list[dict] = []
    try:
        from alpha_agent.analytics.trade_db import get_trades, reconcile_buy_sell_pairs
        reconcile_buy_sell_pairs()   # match SELL rows → close open BUYs (FIFO)
        all_trades   = get_trades(limit=500)
        dt_trades    = [t for t in all_trades if t.get("sleeve") == "DT"]
        scalp_trades = [t for t in all_trades if t.get("sleeve") == "SCALP"]
        trades       = [t for t in all_trades if t.get("sleeve") not in ("DT", "SCALP")]

        # ── Auto-reconcile: marcar como cerradas las posiciones que ya no están en Alpaca
        # Compara BUYs sin closed_at contra las posiciones actuales en Alpaca.
        # Si un ticker no está en Alpaca y tampoco tiene exit price → está cerrado pero sin registrar.
        try:
            current_tickers = {p.ticker for p in positions}
            from alpha_agent.analytics.trade_db import log_trade_close
            import sqlite3
            from pathlib import Path as _P
            _db = _P(__file__).parent / "signals" / "trades.db"
            with sqlite3.connect(str(_db)) as _con:
                _con.row_factory = sqlite3.Row
                open_buys = _con.execute(
                    "SELECT id, ticker, price, date, ts FROM trades "
                    "WHERE side='BUY' AND closed_at IS NULL AND sleeve NOT IN ('DT','SCALP')"
                ).fetchall()
                reconciled = 0
                for row in open_buys:
                    if row["ticker"] not in current_tickers:
                        # Posición ya no está en Alpaca → obtener precio de cierre aproximado
                        try:
                            import yfinance as yf
                            _df = yf.download(row["ticker"], period="2d", progress=False, auto_adjust=True)
                            exit_px = float(_df["Close"].squeeze().iloc[-1]) if _df is not None and len(_df) > 0 else (row["price"] or 0)
                        except Exception:
                            exit_px = row["price"] or 0
                        entry_px = row["price"] or exit_px
                        pnl_usd  = round((exit_px - entry_px), 2)
                        pnl_pct  = round((exit_px - entry_px) / entry_px * 100, 2) if entry_px else 0
                        _con.execute(
                            "UPDATE trades SET closed_at=datetime('now'), exit_price=?, pnl_usd=?, pnl_pct=? WHERE id=?",
                            (exit_px, pnl_usd, pnl_pct, row["id"]),
                        )
                        reconciled += 1
                _con.commit()
            if reconciled:
                logger.info("Auto-reconcile: %d trades marcados como cerrados (no están en Alpaca)", reconciled)
                all_trades = get_trades(limit=500)
                trades     = [t for t in all_trades if t.get("sleeve") not in ("DT", "SCALP")]
        except Exception as _re:
            logger.debug("Auto-reconcile error: %s", _re)

        logger.info("Trades: %d LP/CP, %d DT, %d SCALP", len(trades), len(dt_trades), len(scalp_trades))
    except Exception as e:
        logger.debug("trade_db no disponible: %s", e)

    # Monte Carlo simulation
    mc_result = None
    try:
        from alpha_agent.analytics.montecarlo import run_from_portfolio_history
        mc_result = run_from_portfolio_history(history, initial_capital=equity)
        if mc_result:
            logger.info(
                "Monte Carlo: retorno mediano %.1f%% | DD esperado %.1f%% | prob+ %.0f%%",
                mc_result.median_return_pct, mc_result.expected_max_dd_pct, mc_result.prob_positive_pct,
            )
    except Exception as e:
        logger.debug("Monte Carlo no disponible: %s", e)

    initial = signals_data.get("capital_usd", PARAMS.paper_capital_usd)
    html    = build_html(equity, initial, history, positions, signals_data,
                         spy_history=spy_history, qqq_history=qqq_history,
                         metrics=metrics, perf_data=perf_data, discovery_data=discovery_data,
                         trades=trades, mc_result=mc_result,
                         dt_trades=dt_trades, scalp_trades=scalp_trades,
                         workflow_status=workflow_status, dt_scan=dt_scan_data,
                         brk_history=brk_history)
    OUT_PATH.write_text(html, encoding="utf-8")
    logger.info("Dashboard generado -> %s (%d bytes)", OUT_PATH, len(html))


def _print_health_snapshot() -> int:
    """Snapshot rápido del sistema en texto plain — para CLI / SSH / Cloud Run check.

    Lee:
      - signals/workflow_status.json (last runs Cloud Run)
      - LLM gateway status (calls, costo, providers, cache hit)
      - trade_db (capital reservations, sharpe rolling 30d, open positions)
      - signals/equity_snapshots.json (equity actual + retorno desde baseline)

    Returns: 0 si todo OK, 1 si algún componente crítico está caído.
    """
    import json
    from datetime import datetime, timezone

    print(f"\n=== ALPHA HEALTH — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")
    exit_code = 0

    # 1. Cloud Run workflow status
    try:
        wf_path = BASE_DIR / "signals" / "workflow_status.json"
        if wf_path.exists():
            wf = json.loads(wf_path.read_text(encoding="utf-8"))
            print("\nCloud Run:")
            for job, info in wf.items():
                ts = info.get("ts", "n/a")
                ok = info.get("ok", False)
                marker = "🟢" if ok else "🔴"
                if not ok:
                    exit_code = 1
                print(f"  {marker} {job}: {ts}  ok={ok}")
        else:
            print("\nCloud Run: signals/workflow_status.json no existe")
    except Exception as e:
        print(f"\nCloud Run: error leyendo workflow_status ({e})")

    # 2. LLM gateway
    try:
        from alpha_agent.news.claude_analyst import get_gateway_status
        s = get_gateway_status()
        total_cost = s.get("total_cost_usd", 0.0)
        total_calls = s.get("total_calls", 0)
        anth_pct = s.get("anthropic_budget_used_pct", 0)
        total_pct = s.get("total_budget_used_pct", 0)
        marker = "🟡" if total_pct > 80 else "🟢"
        print(f"\nLLM {marker}: {total_calls} calls / ${total_cost:.4f}  "
              f"(anthropic {anth_pct}%, total {total_pct}% del budget)")
        for pid, info in s.get("providers", {}).items():
            calls = info.get("calls", 0)
            cost = info.get("cost_usd", 0.0)
            hits = info.get("cache_hits", 0)
            print(f"  {pid}: {calls} calls (cache hits {hits}), ${cost:.4f}")
        disabled = s.get("disabled_providers", {})
        if disabled:
            print(f"  ⚠️  Disabled: {list(disabled.keys())}")
    except Exception as e:
        print(f"\nLLM: error ({e})")
        exit_code = 1

    # 3. Capital + sleeve performance
    try:
        from alpha_agent.analytics.trade_db import get_combined_state
        st = get_combined_state(brokers=None)
        res = st.get("capital_reservations", {})
        sleeves = st.get("by_sleeve", {})
        summ = st.get("summary", {})
        print(f"\nCapital reservas: {res if res else '{}'}")
        print(f"Open positions: {summ.get('open_positions', 'n/a')}, "
              f"closed trades: {summ.get('closed_trades', 0)}, "
              f"win rate: {summ.get('win_rate', 'n/a')}")
        if sleeves:
            print("Sharpe rolling 30d por sleeve:")
            for sleeve, stats in sleeves.items():
                print(f"  {sleeve}: n={stats.get('n_trades', 0)} "
                      f"sharpe={stats.get('sharpe', 0):+.2f} "
                      f"avg_pnl={stats.get('avg_pnl', 0):+.2f}% "
                      f"win_rate={stats.get('win_rate', 0):.0%}")
    except Exception as e:
        print(f"\nCapital/sleeves: error ({e})")

    # 4. Equity curve
    try:
        eq_path = BASE_DIR / "signals" / "equity_snapshots.json"
        if eq_path.exists():
            data = json.loads(eq_path.read_text(encoding="utf-8"))
            if isinstance(data, list) and data:
                last = data[-1]
                current = last.get("equity", last.get("v", 0))
                baseline = 1600.0
                pct = (current - baseline) / baseline * 100
                marker = "🟢" if pct >= 0 else "🔴"
                print(f"\nEquity {marker}: ${current:,.2f} (baseline ${baseline:.0f}, "
                      f"{pct:+.1f}% desde inicio, {len(data)} snapshots)")
    except Exception as e:
        print(f"\nEquity: error ({e})")

    print()  # newline final
    return exit_code


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--watch",   action="store_true")
    parser.add_argument("--health",  action="store_true",
                        help="Imprime snapshot de salud del sistema (no genera HTML).")
    args = parser.parse_args()

    load_dotenv(BASE_DIR / ".env")

    if args.health:
        import sys as _sys
        _sys.exit(_print_health_snapshot())

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
