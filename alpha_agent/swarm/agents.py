"""
Agentes especializados del Swarm con Chain-of-Thought.

Cada agente razona paso a paso (→ líneas de pensamiento) antes de emitir
su veredicto final. El formato de output es:

  → [pensamiento 1]
  → [pensamiento 2]
  → [pensamiento 3]
  STANCE|CONFIDENCE|REASONING

donde STANCE ∈ {GO, NO-GO, REDUCE}, CONFIDENCE ∈ [0,100].

Agentes:
  StrategistAgent  → régimen macro estructural, autoriza SHORT
  TechnicalAnalyst → precio, indicadores, estructura, R/R
  SentimentAgent   → noticias, earnings proximity, catalizadores
  RiskAuditor      → refuta al Technical, EV, Kelly sizing

Costo: ~$0.008 por debate completo (Haiku x4 con CoT)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class SwarmOpinion:
    agent:           str
    chain_of_thought: str        # pasos de razonamiento (→ lines)
    stance:          str         # GO | NO-GO | REDUCE
    confidence:      int         # 0-100
    reasoning:       str         # resumen 1 oración
    ev:              float | None = None          # solo RiskAuditor
    ev_data:         dict        = field(default_factory=dict)


# ── helpers ──────────────────────────────────────────────────────────────────

def _haiku(system: str, user: str, max_tokens: int = 500) -> str:
    from anthropic import Anthropic
    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return resp.content[0].text.strip()


def _parse_cot(agent: str, raw: str, ev: float | None = None, ev_data: dict | None = None) -> SwarmOpinion:
    """
    Separa el CoT (→ líneas) del veredicto final (STANCE|CONF|REASON).
    Tolera outputs imperfectos del modelo.
    """
    lines = [ln.strip() for ln in raw.strip().splitlines() if ln.strip()]

    # Buscar la línea de veredicto: la última que contenga | o que empiece con GO/NO-GO/REDUCE
    verdict_line = ""
    cot_lines    = []
    for ln in reversed(lines):
        upper = ln.upper()
        if "|" in ln and any(s in upper for s in ("GO", "NO-GO", "REDUCE")):
            verdict_line = ln
            break
        if upper.startswith(("GO", "NO-GO", "REDUCE")):
            verdict_line = ln
            break

    if verdict_line:
        cot_lines = [ln for ln in lines if ln != verdict_line and ln.startswith("→")]
    else:
        cot_lines    = [ln for ln in lines if ln.startswith("→")]
        verdict_line = lines[-1] if lines else ""

    cot_text = "\n".join(cot_lines) if cot_lines else raw[:300]

    try:
        parts = verdict_line.split("|", 2)
        stance = parts[0].strip().upper()
        if stance not in ("GO", "NO-GO", "REDUCE"):
            stance = "NO-GO"
        confidence = max(0, min(100, int(parts[1].strip()))) if len(parts) > 1 else 50
        reasoning  = parts[2].strip() if len(parts) > 2 else verdict_line[:200]
    except Exception:
        stance, confidence, reasoning = "NO-GO", 40, raw[:200]

    return SwarmOpinion(
        agent=agent,
        chain_of_thought=cot_text,
        stance=stance,
        confidence=confidence,
        reasoning=reasoning,
        ev=ev,
        ev_data=ev_data or {},
    )


# ── Agente 1: Strategist ──────────────────────────────────────────────────────

def strategist_agent(macro: dict, polymarket: dict | None, direction: str) -> SwarmOpinion:
    """
    Determina el régimen estructural del mercado y autoriza la dirección del trade.
    Es el único que puede vetar un SHORT si el contexto macro no lo justifica.
    """
    system = (
        "Sos el Strategist de un hedge fund. Tu rol es determinar el régimen "
        "estructural del mercado (TREND-BULL / TREND-BEAR / RANGE / VOLATILE) "
        "y evaluar si la dirección del trade propuesto tiene sentido en ese contexto.\n\n"
        "Razonamiento requerido:\n"
        "→ [paso 1: qué dice el VIX y el régimen macro]\n"
        "→ [paso 2: qué dice el SPY/sector]\n"
        "→ [paso 3: ¿el régimen justifica esta dirección?]\n"
        "Luego en UNA sola línea: STANCE|CONFIDENCE|REASONING\n"
        "(STANCE = GO / NO-GO / REDUCE, sin texto extra)"
    )
    pm_lines = ""
    if polymarket:
        pm_lines = "\nPolymarket:\n" + "\n".join(
            f"  {k}: {v:.0%}" for k, v in list(polymarket.items())[:5]
        )
    user = (
        f"Dirección propuesta: {direction}\n"
        f"Régimen macro: {macro.get('regime', 'unknown').upper()}\n"
        f"VIX: {macro.get('vix', 0):.1f}\n"
        f"SPY vs VWAP intraday: {macro.get('spy_vwap_dev', 0)*100:+.2f}%\n"
        f"WTI: ${macro.get('wti', 0):.1f} | DXY: {macro.get('dxy', 0):.1f}"
        + pm_lines
    )
    raw = _haiku(system, user)
    return _parse_cot("Strategist", raw)


# ── Agente 2: Technical Analyst ───────────────────────────────────────────────

def technical_analyst(cand: dict, direction: str) -> SwarmOpinion:
    """
    Análisis técnico profundo con contexto estructural.
    No solo reacciona a indicadores: evalúa si la entrada tiene sentido estructural.
    """
    system = (
        "Sos un Technical Analyst institucional especializado en day trading intraday. "
        "No solo listás indicadores: evaluás si el setup tiene sentido estructural "
        "(¿el precio está en el lugar correcto? ¿el volumen confirma? ¿el R/R justifica el riesgo?).\n\n"
        "Razonamiento requerido:\n"
        "→ [paso 1: estructura del precio — gap, VWAP, ORB]\n"
        "→ [paso 2: momentum y convicción — volumen, RSI, señales]\n"
        "→ [paso 3: R/R estructural — ¿el setup justifica la entrada?]\n"
        "Luego en UNA sola línea: STANCE|CONFIDENCE|REASONING\n"
        "(STANCE = GO / NO-GO / REDUCE, sin texto extra)"
    )

    try:
        tp = cand.get("take_profit_2") or cand.get("take_profit_1", 0) or 0
        sl = cand.get("stop_loss", 0) or 0
        px = cand.get("current_price", 1) or 1
        if direction == "SHORT":
            rr = (px - tp) / (sl - px) if (sl - px) > 0 else 0
        else:
            rr = (tp - px) / (px - sl) if (px - sl) > 0 else 0
    except Exception:
        rr = 0.0

    user = (
        f"Ticker: {cand['ticker']} | Dirección: {direction}\n"
        f"Gap vs cierre anterior: {cand.get('gap_pct', 0)*100:+.1f}%\n"
        f"Precio actual: ${cand.get('current_price', 0):.2f}\n"
        f"VWAP deviation: {cand.get('vwap_dev_pct', 0)*100:+.2f}%\n"
        f"ORB score: {cand.get('orb_score', 0):.2f}/1.0 | Vol ratio: {cand.get('vol_ratio', 1):.1f}x\n"
        f"RSI(14): {cand.get('rsi', 50):.0f} | DT score total: {cand.get('dt_score', 0):.3f}\n"
        f"SL: ${cand.get('stop_loss', 0):.2f} | TP1: ${cand.get('take_profit_1', 0):.2f} | "
        f"TP2: ${cand.get('take_profit_2', 0):.2f}\n"
        f"R/R (vs TP2): {rr:.2f}:1"
    )
    raw = _haiku(system, user)
    return _parse_cot("Technical", raw)


# ── Agente 3: Sentiment Agent ─────────────────────────────────────────────────

def sentiment_agent(cand: dict, headlines: list[str], earnings_days: int | None) -> SwarmOpinion:
    """
    Evalúa el contexto noticioso y catalizadores cualitativos.
    Foco especial en earnings proximity (riesgo de gap adverso).
    """
    system = (
        "Sos un analista de sentiment e información cualitativa. "
        "Tu especialidad es detectar cuándo el contexto noticioso INVALIDA un setup "
        "técnico válido (earnings inminentes, escándalo corporativo, cambio regulatorio).\n\n"
        "Razonamiento requerido:\n"
        "→ [paso 1: riesgo de earnings — ¿hay fecha en los próximos días?]\n"
        "→ [paso 2: calidad y dirección de las noticias]\n"
        "→ [paso 3: ¿el sentiment refuerza o invalida el setup técnico?]\n"
        "Luego en UNA sola línea: STANCE|CONFIDENCE|REASONING\n"
        "(STANCE = GO / NO-GO / REDUCE, sin texto extra)"
    )

    hl = "\n".join(f"- {h}" for h in headlines[:5]) if headlines else "Sin noticias disponibles."
    if earnings_days is not None and earnings_days <= 2:
        earn_warn = f"⚠️ EARNINGS EN {earnings_days} DÍA(S) — riesgo gap adverso severo"
    elif earnings_days is not None and earnings_days <= 5:
        earn_warn = f"Earnings en {earnings_days} días — riesgo moderado de gap"
    else:
        earn_warn = "Sin earnings próximos (ventana de 5 días limpia)"

    user = f"Ticker: {cand['ticker']}\n{earn_warn}\nNoticias recientes:\n{hl}"
    raw = _haiku(system, user)
    return _parse_cot("Sentiment", raw)


# ── Agente 4: Risk Auditor (adversarial) ─────────────────────────────────────

def risk_auditor(
    cand: dict,
    technical_opinion: SwarmOpinion,
    portfolio_heat: float,
    pnl_today: float,
    trades_today: int,
    ev_data: dict,
) -> SwarmOpinion:
    """
    El Risk Auditor recibe el análisis completo del Technical Analyst y
    su trabajo es REFUTARLO activamente buscando las fallas del argumento.

    Solo apoya si el EV es positivo Y no encuentra falla fatal en el análisis técnico.
    """
    system = (
        "Sos el Risk Auditor de un hedge fund, el 'Gatekeeper' del sistema. "
        "Tu trabajo es REFUTAR el análisis del Technical Analyst buscando sus puntos débiles. "
        "Sos el más conservador del equipo, pero tu conservadurismo se basa en lógica, "
        "no en miedo. Si el EV es positivo y el setup es sólido, apoyás el trade con size adecuado.\n\n"
        "Razonamiento requerido:\n"
        "→ [paso 1: ¿el análisis técnico tiene fallas? ¿qué asume que podría estar mal?]\n"
        "→ [paso 2: EV y Kelly — ¿el tamaño es matemáticamente justificado?]\n"
        "→ [paso 3: contexto de portfolio — heat, P&L del día, regla 1-trade-DT]\n"
        "Luego en UNA sola línea: STANCE|CONFIDENCE|REASONING\n"
        "(STANCE = GO / NO-GO / REDUCE, sin texto extra)"
    )

    tech_summary = (
        f"ANÁLISIS DEL TECHNICAL ANALYST:\n"
        f"{technical_opinion.chain_of_thought}\n"
        f"→ Veredicto: {technical_opinion.stance} ({technical_opinion.confidence}%) — {technical_opinion.reasoning}"
    )

    ev_lines = (
        f"Expected Value del trade: ${ev_data.get('ev', 0):+.2f}\n"
        f"  Win rate ({ev_data.get('source', '?')}): {ev_data.get('win_rate', 0)*100:.1f}%\n"
        f"  Avg win: ${ev_data.get('avg_win', 0):.2f} | Avg loss: ${ev_data.get('avg_loss', 0):.2f}\n"
        f"  Kelly óptimo (half): {ev_data.get('kelly_fraction', 0)*100:.1f}% del capital"
    )

    user = (
        f"Ticker: {cand['ticker']} | Notional: ${cand.get('notional', 0):.0f}\n"
        f"SL: ${cand.get('stop_loss', 0):.2f} (-1.5%) | "
        f"TP1: ${cand.get('take_profit_1', 0):.2f} (+3%) | "
        f"TP2: ${cand.get('take_profit_2', 0):.2f} (+7%)\n\n"
        f"{tech_summary}\n\n"
        f"{ev_lines}\n\n"
        f"Estado del portfolio:\n"
        f"  Portfolio heat: {portfolio_heat:.1f}%\n"
        f"  P&L realizado hoy: ${pnl_today:+.0f}\n"
        f"  Trades DT ejecutados hoy: {trades_today} (máx 1 por día)\n"
        f"  Pérdida máxima posible este trade: ${cand.get('notional', 0)*0.015:.0f}"
    )
    raw = _haiku(system, user)
    ev_val = ev_data.get("ev")
    return _parse_cot("RiskAuditor", raw, ev=ev_val, ev_data=ev_data)
