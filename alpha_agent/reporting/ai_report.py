"""
Reporte ejecutivo en lenguaje natural.

Dos modos:
    - `signals_to_compact_brief`: brief determinístico a partir del JSON ya
      estructurado. No depende de Gemini.
    - `generate_executive_report`: pasa el JSON + contexto macro a Gemini y
      le pide una narrativa pulida para WhatsApp. Fallback automático al brief
      si falla.
"""

from __future__ import annotations

import logging
import os

from alpha_agent.config import GEMINI_MODEL
from alpha_agent.reporting.signals import Signals

logger = logging.getLogger(__name__)


def _format_macro(macro: dict) -> str:
    regime = macro.get("regime", "unknown").upper()
    reason = macro.get("regime_reason", "")
    prices = macro.get("prices", {})
    chg = macro.get("changes_1m", {})

    bits = [f"Régimen: {regime}"]
    if reason:
        bits.append(f"({reason})")
    if "vix" in prices:
        bits.append(f"VIX {prices['vix']:.1f}")
    if "oil_wti" in prices:
        oil_chg = chg.get("oil_wti", 0)
        bits.append(f"WTI ${prices['oil_wti']:.1f} ({oil_chg*100:+.1f}% 1m)")
    if "gold" in prices:
        gold_chg = chg.get("gold", 0)
        bits.append(f"Oro ${prices['gold']:.0f} ({gold_chg*100:+.1f}% 1m)")
    if "dxy" in prices:
        dxy_chg = chg.get("dxy", 0)
        bits.append(f"DXY {prices['dxy']:.1f} ({dxy_chg*100:+.1f}% 1m)")
    return " · ".join(bits)


def _format_signal(s: dict, capital: float) -> str:
    thesis = s.get("thesis", {})
    conv = thesis.get("conviction", "?")
    risk = thesis.get("risk", {})
    dollars = risk.get("dollars_allocated", 0)
    max_loss = risk.get("max_loss_usd_if_stop_hit", 0)
    sl = s.get("stop_loss")
    tp = s.get("take_profit")

    lines = [
        f"• {s['ticker']} [{conv}] — BUY ${s['price']} → ${dollars:.2f} ({s['weight_target']*100:.1f}% sleeve)",
        f"   SL ${sl} · TP ${tp} · riesgo máx ${max_loss:.2f}",
    ]
    thesis_text = thesis.get("thesis_text", "")
    if thesis_text:
        # cortar a ~2 oraciones para que entre en WhatsApp
        short = ". ".join(thesis_text.split(". ")[:2])
        if not short.endswith("."):
            short += "."
        lines.append(f"   ↳ {short}")
    risks = thesis.get("key_risks", [])
    if risks:
        lines.append(f"   ⚠ {risks[0]}")
    return "\n".join(lines)


def _format_option_signal(s: dict) -> str:
    opt = s.get("option") or {}
    t = opt.get("type", "?").upper()
    strike = opt.get("strike", "?")
    exp = opt.get("expiry", "?")
    prem = opt.get("contract_cost_est", 0)
    role = opt.get("role", "directional")
    role_tag = "HEDGE" if role == "portfolio_hedge" else "DIR"
    return (
        f"• [{role_tag}] {t} {s['ticker']} @ ${strike} exp {exp} "
        f"→ ~${prem:.0f}/contrato (riesgo máx = prima)"
    )


def signals_to_compact_brief(signals: Signals) -> str:
    p = signals.params
    wlp = p.get("weight_long_term", 0.55)
    wst = p.get("weight_short_term", 0.25)
    wopt = p.get("weight_options", 0.20)
    out = [
        f"📊 REPORTE QUANT — {signals.generated_at}",
        f"Capital: ${signals.capital_usd:.0f} USD",
        f"Sleeves: {wlp*100:.0f}% LP / {wst*100:.0f}% CP / {wopt*100:.0f}% OPT",
        "",
        _format_macro(signals.macro),
        "",
    ]
    if signals.long_term:
        out.append(f"🟢 LARGO PLAZO (sleeve {wlp*100:.0f}%)")
        for s in signals.long_term:
            out.append(_format_signal(asdict_compat(s), signals.capital_usd))
        out.append("")
    if signals.short_term:
        out.append(f"🟡 CORTO PLAZO (sleeve {wst*100:.0f}%)")
        for s in signals.short_term:
            out.append(_format_signal(asdict_compat(s), signals.capital_usd))
        out.append("")
    if signals.options_book:
        out.append(f"🔶 OPCIONES DIRECCIONALES (sleeve {wopt*100:.0f}%)")
        for s in signals.options_book:
            out.append(_format_option_signal(asdict_compat(s)))
        out.append("")
    if signals.hedge_book:
        out.append("🛡 HEDGE DE CARTERA")
        for s in signals.hedge_book:
            out.append(_format_option_signal(asdict_compat(s)))
    return "\n".join(out)


def asdict_compat(s):
    """Tolera Signal dataclass o dict."""
    if isinstance(s, dict):
        return s
    from dataclasses import asdict
    return asdict(s)


# ═══════════════════════════════════════════════════════════════════════════
# WHATSAPP BRIEF — versión simplificada, scannable en 5 segundos
# ═══════════════════════════════════════════════════════════════════════════

def _trend_line(label: str, pct: float) -> str:
    """Formatea una línea de tendencia con flecha sólo si hay movimiento real."""
    if pct >= 0.02:
        return f"{label} ↑ {pct*100:+.1f}%"
    if pct <= -0.02:
        return f"{label} ↓ {pct*100:+.1f}%"
    return f"{label} estable ({pct*100:+.1f}%)"


def _macro_trends(macro: dict) -> list[str]:
    """Arma las líneas de tendencia macro del último mes."""
    chg = macro.get("changes_1m", {})
    prices = macro.get("prices", {})
    lines = []
    if "oil_wti" in prices:
        lines.append(_trend_line("Petróleo", chg.get("oil_wti", 0)))
    if "gold" in prices:
        lines.append(_trend_line("Oro", chg.get("gold", 0)))
    if "dxy" in prices:
        lines.append(_trend_line("Dólar", chg.get("dxy", 0)))
    return lines


def _macro_reading(macro: dict) -> str:
    """Una frase que interpreta qué está pasando en el mercado."""
    regime = macro.get("regime", "unknown")
    chg = macro.get("changes_1m", {})
    oil = chg.get("oil_wti", 0)
    gold = chg.get("gold", 0)
    dxy = chg.get("dxy", 0)
    vix = macro.get("prices", {}).get("vix", 20)

    if regime == "bull" and oil > 0.05 and gold < -0.03:
        return "Rotación de refugio hacia energía/recursos."
    if regime == "bull" and vix < 18:
        return "Apetito por riesgo firme, volatilidad baja."
    if regime == "bear" or vix > 25:
        return "Aversión al riesgo — priorizar capital sobre rendimiento."
    if dxy > 0.02:
        return "Dólar fuerte — presión sobre emergentes y commodities."
    if gold > 0.05:
        return "Búsqueda de refugio — incertidumbre creciente."
    return "Mercado en consolidación sin catalizador claro."


def _top_headlines(signals: Signals, max_headlines: int = 3) -> list[str]:
    """
    Extrae los titulares más relevantes de las tesis ya construidas.
    Prioriza noticias de tickers con mayor peso en la cartera.
    """
    seen: set[str] = set()
    headlines: list[tuple[float, str, str]] = []   # (peso, ticker, titular)

    all_signals = list(signals.long_term) + list(signals.short_term) + list(signals.options_book)
    for s in all_signals:
        sd = asdict_compat(s)
        fund = sd.get("thesis", {}).get("fundamental", {})
        titles = fund.get("sample_titles") or []
        weight = sd.get("weight_target", 0) or 0
        ticker = sd.get("ticker", "?")
        for title in titles[:2]:
            if not title or title in seen:
                continue
            seen.add(title)
            # Limpiar encoding raro
            clean = title.strip()
            if len(clean) > 90:
                clean = clean[:87] + "..."
            headlines.append((float(weight), ticker, clean))

    headlines.sort(key=lambda x: x[0], reverse=True)
    return [f"{t}: {h}" for _, t, h in headlines[:max_headlines]]


def _format_decision_line_lp(s: dict) -> list[str]:
    """
    Dos líneas por posición de LARGO PLAZO. Formato simple sin padding
    para que tickers largos (PAMP.BA) no rompan el render en WhatsApp:
      🟢 RTX $201.56 → $365
         ER +14% · α +8% · Sh 1.4 · calidad + alfa elevado
    """
    ticker = s["ticker"]
    thesis = s.get("thesis", {})
    conv = thesis.get("conviction", "")
    conv_tag = {"ALTA": "🟢", "MEDIA": "🟡", "BAJA": "🔴"}.get(conv, "·")
    price = s.get("price", 0)
    risk = thesis.get("risk", {})
    dollars = risk.get("dollars_allocated", 0)
    q = thesis.get("quant", {})
    er = q.get("expected_return_capm", 0) * 100
    alpha = q.get("alpha_jensen", 0) * 100
    sharpe = q.get("sharpe", 0)
    line1 = f"  {conv_tag} {ticker} ${price:.2f} → ${dollars:.0f}"
    reason = _short_reason(s)
    line2 = f"     ER {er:+.0f}% · α {alpha:+.0f}% · Sh {sharpe:.1f}"
    if reason:
        line2 += f" · {reason}"
    return [line1, line2]


def _format_decision_line_cp(s: dict) -> list[str]:
    """
    Dos líneas por posición CORTO PLAZO:
      🟡 PBR $21.51 → $237
         RSI 32 · mom +4% · TP +14% · sobreventa → rebote
    """
    ticker = s["ticker"]
    thesis = s.get("thesis", {})
    conv = thesis.get("conviction", "")
    conv_tag = {"ALTA": "🟢", "MEDIA": "🟡", "BAJA": "🔴"}.get(conv, "·")
    price = s.get("price", 0)
    risk = thesis.get("risk", {})
    dollars = risk.get("dollars_allocated", 0)
    tech = thesis.get("technical", {})
    rsi = tech.get("rsi", 0)
    ret_1m = tech.get("ret_1m", 0) * 100
    tp = s.get("take_profit") or 0
    tp_upside = ((tp - price) / price * 100) if price else 0
    line1 = f"  {conv_tag} {ticker} ${price:.2f} → ${dollars:.0f}"
    reason = _short_reason(s)
    line2 = f"     RSI {rsi:.0f} · mom {ret_1m:+.0f}% · TP {tp_upside:+.0f}%"
    if reason:
        line2 += f" · {reason}"
    return [line1, line2]


def _format_option_line(s: dict) -> str:
    opt = s.get("option") or {}
    t = opt.get("type", "?").upper()
    strike = opt.get("strike", 0)
    prem = opt.get("contract_cost_est", 0)
    exp = opt.get("expiry", "?")
    return f"  🔶 {t} {s['ticker']} K${strike:.2f} → ${prem:.0f}/contr · exp {exp}"


def _short_reason(s: dict) -> str:
    """
    Frase ultra-corta (≤55 chars) que justifica la posición.
    Prioriza: sentiment del ticker > RSI extremo > alfa > sector macro.
    """
    thesis = s.get("thesis", {})
    horizon = thesis.get("horizon", "LP")
    q = thesis.get("quant", {})
    t = thesis.get("technical", {})
    f = thesis.get("fundamental", {})
    sent = f.get("sentiment_score", 0) or 0
    n = f.get("n_headlines", 0) or 0
    rsi = t.get("rsi", 50) or 50
    alpha = q.get("alpha_jensen", 0) or 0
    sharpe = q.get("sharpe", 0) or 0

    bits = []
    if horizon == "CP":
        if rsi < 35:
            bits.append("sobreventa → rebote")
        elif rsi > 70:
            bits.append("momentum fuerte")
        else:
            bits.append("setup técnico neutro")
    else:
        if sharpe >= 1.0 and alpha > 0.03:
            bits.append("calidad + alfa elevado")
        elif sharpe >= 1.0:
            bits.append("Sharpe sólido")
        elif alpha > 0.05:
            bits.append("alfa idiosincrático")
        else:
            bits.append("diversificador de cartera")
    if sent > 0.3 and n >= 2:
        bits.append("news +")
    elif sent < -0.3 and n >= 2:
        bits.append("news −")
    out = " · ".join(bits)
    return out[:55]


def _find_top_opportunity(signals: Signals) -> str | None:
    """Encuentra la mejor oportunidad: mayor Sharpe entre LP + CP."""
    best = None
    best_score = -999
    for s in list(signals.long_term) + list(signals.short_term):
        sd = asdict_compat(s)
        q = sd.get("thesis", {}).get("quant", {})
        sharpe = q.get("sharpe", 0) or 0
        alpha = q.get("alpha_jensen", 0) or 0
        score = sharpe + alpha * 10   # alfa pesa más
        if score > best_score:
            best_score = score
            best = sd
    if not best:
        return None
    q = best.get("thesis", {}).get("quant", {})
    return (f"{best['ticker']} (Sharpe {q.get('sharpe', 0):.2f} · "
            f"α {q.get('alpha_jensen', 0)*100:+.1f}%)")


_SECTOR_MAP = {
    "ENERGÍA": {"XOM", "CVX", "PBR", "SLB", "SHEL", "TTE", "VIST", "YPF", "PAM",
                "PAMP.BA", "TGS", "EDN"},
    "MATERIALES/ORO": {"GOLD", "NEM", "RIO", "VALE", "FCX", "GLD"},
    "TECH": {"NVDA", "AMD", "MSFT", "GOOGL", "AAPL", "META", "TSLA", "TSM", "ASML", "PLTR"},
    "DEFENSA": {"RTX", "LMT", "NOC", "GD", "BA"},
    "FINANCIERO": {"JPM", "BAC", "GS", "MS", "GGAL", "BMA"},
    "EMERGENTES LATAM": {"PBR", "VIST", "YPF", "VALE", "MELI", "DESP", "GGAL", "BMA",
                         "PAMP.BA", "ALUA.BA", "IRS"},
}


def _detect_sector_bias(signals: Signals, macro: dict) -> str | None:
    """
    Detecta sesgo sectorial en LP + CP y lo anota con contexto macro.
    Por ejemplo: 'Sobrepeso energía coherente con WTI +10%'.
    """
    from collections import Counter
    counts: Counter = Counter()
    for s in list(signals.long_term) + list(signals.short_term):
        ticker = (asdict_compat(s)).get("ticker", "")
        for sector, members in _SECTOR_MAP.items():
            if ticker in members:
                counts[sector] += 1
    if not counts:
        return None
    top_sector, top_n = counts.most_common(1)[0]
    if top_n < 2:
        return None
    chg = macro.get("changes_1m", {})
    ctx = ""
    if top_sector == "ENERGÍA" and chg.get("oil_wti", 0) > 0.05:
        ctx = f" (coherente con WTI {chg['oil_wti']*100:+.0f}%)"
    elif top_sector == "MATERIALES/ORO" and chg.get("gold", 0) < -0.05:
        ctx = f" (contracíclico al oro {chg['gold']*100:+.0f}%)"
    elif top_sector == "TECH" and macro.get("regime") == "bull":
        ctx = " (régimen bull favorece duration)"
    elif top_sector == "EMERGENTES LATAM" and chg.get("dxy", 0) < 0:
        ctx = " (dólar débil ayuda EM)"
    return f"Sesgo: {top_sector} ({top_n} posiciones){ctx}"


def _portfolio_expected_return(signals: Signals) -> tuple[float, float]:
    """
    Retorno esperado agregado del portafolio en USD + %.
    LP → CAPM 12m expected return
    CP → take_profit implícito (tope conservador)
    Opciones → no se suman (cash flow incierto sin delta)
    """
    total_er_usd = 0.0
    total_alloc = 0.0
    for s in signals.long_term:
        sd = asdict_compat(s)
        risk = sd.get("thesis", {}).get("risk", {})
        q = sd.get("thesis", {}).get("quant", {})
        alloc = risk.get("dollars_allocated", 0) or 0
        er = q.get("expected_return_capm", 0) or 0
        total_er_usd += alloc * er
        total_alloc += alloc
    for s in signals.short_term:
        sd = asdict_compat(s)
        risk = sd.get("thesis", {}).get("risk", {})
        alloc = risk.get("dollars_allocated", 0) or 0
        price = sd.get("price", 0) or 0
        tp = sd.get("take_profit") or 0
        # Pondera TP con probabilidad 50% (conservador)
        upside = ((tp - price) / price) if price else 0
        total_er_usd += alloc * upside * 0.5
        total_alloc += alloc
    return total_er_usd, total_alloc


def _total_allocated(signals: Signals) -> tuple[float, float]:
    """Devuelve (total asignado en USD, pérdida máx teórica)."""
    total_alloc = 0.0
    total_risk = 0.0
    for group in (signals.long_term, signals.short_term):
        for s in group:
            sd = asdict_compat(s)
            risk = sd.get("thesis", {}).get("risk", {})
            total_alloc += risk.get("dollars_allocated", 0) or 0
            total_risk += risk.get("max_loss_usd_if_stop_hit", 0) or 0
    for group in (signals.options_book, signals.hedge_book):
        for s in group:
            sd = asdict_compat(s)
            opt = sd.get("option") or {}
            cost = opt.get("contract_cost_est", 0) or 0
            total_alloc += cost
            total_risk += cost     # opciones long: pérdida máx = prima
    return total_alloc, total_risk


def signals_to_whatsapp_brief(signals: Signals) -> str:
    """
    Reporte ejecutivo para WhatsApp. ~1800 chars, scannable pero con
    razonamiento por posición (Sharpe, alfa, RSI), hallazgos del scan
    y proyección riesgo/retorno del portafolio.

    Estructura:
        header · régimen → tendencias macro → hallazgos → eventos →
        decisiones por sleeve → proyección R/R → protección capital
    """
    macro = signals.macro
    regime = macro.get("regime", "unknown").upper()
    regime_emoji = {"BULL": "🟢", "BEAR": "🔴", "SIDEWAYS": "🟡"}.get(regime, "⚪")
    vix = macro.get("prices", {}).get("vix", 0)

    # Fecha corta
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(signals.generated_at)
        fecha = dt.strftime("%d-%b %H:%M").lower()
    except Exception:
        fecha = signals.generated_at

    total_alloc, total_risk = _total_allocated(signals)
    cap = signals.capital_usd or 1
    risk_pct = (total_risk / cap) * 100
    er_usd, _ = _portfolio_expected_return(signals)
    er_pct = (er_usd / cap) * 100 if cap else 0
    # Riesgo "esperado" = peor caso ponderado 30% (asume ~1/3 de posiciones fallan)
    risk_esperado = total_risk * 0.30
    rr_ratio = (er_usd / risk_esperado) if risk_esperado > 0 else 0
    rr_tag = "✓ favorable" if rr_ratio >= 1.0 else ("neutro" if rr_ratio >= 0.5 else "⚠ defensivo")

    # Sleeves params
    p = signals.params
    wlp = p.get("weight_long_term", 0.55)
    wst = p.get("weight_short_term", 0.25)
    wopt = p.get("weight_options", 0.20)

    out = []
    # ── Header ──
    out.append(f"🤖 *ALPHA* · {fecha}")
    out.append(f"${cap:.0f} USD · {regime_emoji} {regime} · VIX {vix:.0f}")
    out.append("")

    # ── Tendencias macro ──
    out.append("📈 *MERCADO (últ. mes)*")
    for line in _macro_trends(macro):
        out.append(f"  {line}")
    out.append(f"  _{_macro_reading(macro)}_")
    out.append("")

    n_total = (len(signals.long_term) + len(signals.short_term) +
               len(signals.options_book) + len(signals.hedge_book))

    # ── Radar de mercado — movers + noticias del universo completo ──
    radar_data = signals.radar or {}
    radar_entries = radar_data.get("entries", [])
    if radar_entries:
        n_up = radar_data.get("n_up", 0)
        n_down = radar_data.get("n_down", 0)
        out.append(f"📡 *RADAR* ({n_up}↑ {n_down}↓ del universo)")
        for re in radar_entries[:8]:
            tkr = re.get("ticker", "?")
            pct_1d = re.get("pct_1d", 0) * 100
            pct_1w = re.get("pct_1w", 0) * 100
            action = re.get("action", "—")
            headline = re.get("headline", "")
            sent = re.get("sentiment", 0)
            # Emoji de dirección diaria
            if pct_1d >= 2:
                dir_emoji = "🔥"
            elif pct_1d >= 0.5:
                dir_emoji = "↑"
            elif pct_1d <= -2:
                dir_emoji = "💥"
            elif pct_1d <= -0.5:
                dir_emoji = "↓"
            else:
                dir_emoji = "·"
            # Emoji de action del bot
            act_tag = {"BUY_LP": "🟢LP", "BUY_CP": "🟡CP", "CALL": "🔶C",
                       "PUT": "🔶P", "HEDGE": "🛡H"}.get(action, "")
            line = f"  {dir_emoji} {tkr} {pct_1d:+.1f}%d {pct_1w:+.1f}%w"
            if act_tag:
                line += f" → {act_tag}"
            out.append(line)
            if headline:
                sent_tag = "🟢" if sent > 0.25 else ("🔴" if sent < -0.25 else "")
                out.append(f"     {sent_tag}{headline}")
        out.append("")

    # ── Hallazgos del scan ──
    out.append("🧠 *HALLAZGOS*")
    top = _find_top_opportunity(signals)
    if top:
        out.append(f"  • Mejor pick: {top}")
    # Alfa promedio de LP (cuánto "paga" la cartera sobre el riesgo sistémico)
    lp_alphas = [(asdict_compat(s).get("thesis", {}).get("quant", {}).get("alpha_jensen", 0) or 0)
                 for s in signals.long_term]
    if lp_alphas:
        avg_alpha = sum(lp_alphas) / len(lp_alphas) * 100
        out.append(f"  • Alfa promedio LP: {avg_alpha:+.1f}% sobre CAPM")
    # Sesgo sectorial detectado (sobrepeso vs macro)
    sector_hint = _detect_sector_bias(signals, macro)
    if sector_hint:
        out.append(f"  • {sector_hint}")
    out.append("")

    # ── Eventos clave ──
    headlines = _top_headlines(signals, max_headlines=3)
    if headlines:
        out.append("📰 *EVENTOS CLAVE*")
        for h in headlines:
            out.append(f"  • {h}")
        out.append("")

    # ── Decisiones ──
    out.append(f"🎯 *DECISIONES* ({n_total} movimientos · ${total_alloc:.0f} asignado)")
    out.append("")

    if signals.long_term:
        lp_alloc = sum((asdict_compat(s).get("thesis", {}).get("risk", {})
                        .get("dollars_allocated", 0) or 0) for s in signals.long_term)
        out.append(f" 🟢 LARGO PLAZO (${lp_alloc:.0f} · {wlp*100:.0f}%)")
        for s in signals.long_term:
            out.extend(_format_decision_line_lp(asdict_compat(s)))
        out.append("")

    if signals.short_term:
        cp_alloc = sum((asdict_compat(s).get("thesis", {}).get("risk", {})
                        .get("dollars_allocated", 0) or 0) for s in signals.short_term)
        out.append(f" 🟡 CORTO PLAZO (${cp_alloc:.0f} · {wst*100:.0f}%)")
        for s in signals.short_term:
            out.extend(_format_decision_line_cp(asdict_compat(s)))
        out.append("")

    if signals.options_book:
        opt_alloc = sum(((asdict_compat(s).get("option") or {})
                         .get("contract_cost_est", 0) or 0) for s in signals.options_book)
        out.append(f" 🔶 OPCIONES (${opt_alloc:.0f} · {wopt*100:.0f}%)")
        for s in signals.options_book:
            out.append(_format_option_line(asdict_compat(s)))
        out.append("")

    if signals.hedge_book:
        hedge_alloc = sum(((asdict_compat(s).get("option") or {})
                           .get("contract_cost_est", 0) or 0) for s in signals.hedge_book)
        out.append(f" 🛡 HEDGE (${hedge_alloc:.0f})")
        for s in signals.hedge_book:
            out.append(_format_option_line(asdict_compat(s)))
        out.append("")
    else:
        out.append(" 🛡 HEDGE: no requerido en régimen actual")
        out.append("")

    # ── Proyección riesgo/retorno ──
    out.append("💰 *PROYECCIÓN 12m*")
    out.append(f"  Retorno esperado: +${er_usd:.0f} ({er_pct:+.1f}%)")
    out.append(f"  Riesgo esperado: -${risk_esperado:.0f} (-{risk_esperado/cap*100:.1f}%)")
    out.append(f"  Peor caso teórico: -${total_risk:.0f} (-{risk_pct:.1f}%)")
    if rr_ratio > 0:
        out.append(f"  Retorno/Riesgo: {rr_ratio:.1f}x {rr_tag}")
    out.append("")

    # ── Protección de capital ──
    out.append("*PROTECCION DE CAPITAL*")
    out.append(f"  Kill switch intraday: -3% del equity")
    out.append(f"  Stop loss ATR por posicion (LP y CP)")
    out.append(f"  Opciones long-only (perdida acotada a prima)")

    # Earnings warning — tickers con resultados inminentes
    all_tickers = [s.ticker for s in signals.long_term + signals.short_term]
    if all_tickers:
        try:
            from alpha_agent.analytics.earnings_guard import get_earnings_soon
            upcoming = get_earnings_soon(all_tickers, days=5)
            if upcoming:
                pairs = ", ".join(f"{t} ({d.strftime('%d/%b')})" for t, d in upcoming.items())
                out.append(f"  EARNINGS proximos: {pairs} — no abrir nuevas posiciones")
        except Exception:
            pass

    out.append(f"  _Optimizacion: maximizar retorno sujeto a DD <= 10%_")
    out.append("")

    out.append(f"_Próxima revisión: lun-vie 10:35 ART_")

    return "\n".join(out)


def generate_executive_report(signals: Signals) -> str:
    """Genera reporte ejecutivo con Gemini a partir del Signals enriquecido."""
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        logger.warning("GOOGLE_API_KEY no seteada — usando brief determinístico.")
        return signals_to_compact_brief(signals)

    try:
        import google.generativeai as genai  # type: ignore
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(GEMINI_MODEL)

        prompt = f"""
Sos un Chief Investment Officer senior asesorando una cuenta paper de
${signals.capital_usd:.0f} USD. El motor cuantitativo ya corrió CAPM, Markowitz,
analizó noticias por activo y el contexto macro. Tu única tarea es redactar
un reporte ejecutivo EN ESPAÑOL RIOPLATENSE para WhatsApp.

DATOS (JSON estructurado):
{signals.to_json()}

REQUERIMIENTOS DEL REPORTE (formato WhatsApp, máx 1400 caracteres, sin markdown pesado):
1. Una frase de apertura con la lectura del régimen de mercado.
2. Para cada posición LP: ticker, USD asignado, stop loss, y UNA razón financiera
   fuerte (Sharpe + contexto de noticias o macro). No repitas el thesis_text tal cual.
3. Para cada posición CP: ticker, setup técnico específico, stop loss, riesgo USD.
4. Un bloque de 1-2 riesgos macro a monitorear esta semana.
5. Cerrá con una línea de convicción general (alta/media/baja) y qué la cambiaría.

Usá viñetas con "•", tono profesional pero directo, sin emojis más allá de los ya incluidos.
"""
        resp = model.generate_content(prompt)
        return resp.text
    except Exception as e:
        logger.error("Fallo Gemini: %s — fallback a brief determinístico.", e)
        return signals_to_compact_brief(signals)
