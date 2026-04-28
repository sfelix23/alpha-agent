"""
Decision Committee — 4 agentes especializados + 1 meta-agente.

Transforma el sistema de 'seguidor de reglas' a 'razonador'.
Cada agente analiza una dimension y retorna su opinion estructurada.
El meta-agente integra las 4 opiniones y emite la decision final.

Agentes:
  TechnicalAgent  → price action, indicadores, ORB, VWAP
  MacroAgent      → regimen, VIX, Polymarket, sector rotation
  RiskAgent       → sizing, portfolio heat, historial reciente
  SentimentAgent  → noticias, earnings proximity, catalyst

Costo por decision: ~$0.01 (Haiku x4 + Sonnet x1)
Latencia: ~5-8 segundos (paralelo)
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class AgentOpinion:
    agent: str
    stance: str       # "GO" | "NO-GO" | "REDUCE"
    confidence: int   # 0-100
    reasoning: str


@dataclass
class CommitteeDecision:
    go: bool
    size_factor: float          # 1.0 = tamaño completo, 0.5 = mitad
    reasoning: str              # explicacion del meta-agente en español
    opinions: list[AgentOpinion] = field(default_factory=list)
    go_count: int = 0


# ── helpers ──────────────────────────────────────────────────────────────────

def _haiku(system: str, user: str, max_tokens: int = 300) -> str:
    from anthropic import Anthropic
    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return resp.content[0].text.strip()


def _parse(agent: str, text: str) -> AgentOpinion:
    """Parsea 'STANCE|CONFIDENCE|REASONING' devuelto por cada agente."""
    try:
        parts = text.split("|", 2)
        stance = parts[0].strip().upper()
        if stance not in ("GO", "NO-GO", "REDUCE"):
            stance = "NO-GO"
        confidence = max(0, min(100, int(parts[1].strip())))
        reasoning  = parts[2].strip() if len(parts) > 2 else text
        return AgentOpinion(agent=agent, stance=stance, confidence=confidence, reasoning=reasoning)
    except Exception:
        return AgentOpinion(agent=agent, stance="NO-GO", confidence=40, reasoning=text[:200])


# ── agentes especializados ────────────────────────────────────────────────────

def _technical_agent(cand: dict, direction: str = "LONG") -> AgentOpinion:
    system = (
        "Sos un trader institucional especialista en analisis tecnico intraday. "
        "Evaluas si el setup tecnico de un trade es valido. "
        "Respondé EXACTAMENTE: STANCE|CONFIDENCE|REASONING "
        "(STANCE = GO / NO-GO / REDUCE, CONFIDENCE = 0-100, "
        "REASONING = 1-2 oraciones en español). Sin texto extra."
    )
    rr = 0.0
    try:
        tp = cand.get("take_profit_2") or cand.get("take_profit", 0)
        sl = cand.get("stop_loss", 0)
        px = cand.get("current_price", 1)
        if direction == "SHORT":
            rr = (px - tp) / (sl - px) if (sl - px) > 0 else 0
        else:
            rr = (tp - px) / (px - sl) if (px - sl) > 0 else 0
    except Exception:
        pass

    user = (
        f"Ticker: {cand['ticker']} | Direccion: {direction}\n"
        f"Gap vs cierre: {cand.get('gap_pct',0)*100:+.1f}%\n"
        f"Precio actual: ${cand.get('current_price',0):.2f}\n"
        f"VWAP deviation: {cand.get('vwap_dev_pct',0)*100:+.2f}%\n"
        f"ORB score: {cand.get('orb_score',0):.2f}/1.0\n"
        f"Vol ratio: {cand.get('vol_ratio',1):.1f}x\n"
        f"RSI(14): {cand.get('rsi',50):.0f}\n"
        f"DT score: {cand.get('dt_score',0):.3f}\n"
        f"R/R ratio: {rr:.2f}:1"
    )
    return _parse("Technical", _haiku(system, user))


def _macro_agent(cand: dict, macro: dict, polymarket: dict | None) -> AgentOpinion:
    system = (
        "Sos un macro strategist especialista en contexto de mercado para day trading. "
        "Evaluas si el entorno macro es favorable para el trade propuesto. "
        "Respondé EXACTAMENTE: STANCE|CONFIDENCE|REASONING"
    )
    pm_lines = ""
    if polymarket:
        pm_lines = "\nPolymarket (probabilidades reales):\n" + "\n".join(
            f"  {k}: {v:.0%}" for k, v in list(polymarket.items())[:5]
        )
    user = (
        f"Ticker: {cand['ticker']}\n"
        f"Regimen macro: {macro.get('regime','unknown').upper()}\n"
        f"VIX: {macro.get('vix',0):.1f}\n"
        f"SPY vs VWAP intraday: {macro.get('spy_vwap_dev',0)*100:+.2f}%\n"
        f"WTI: ${macro.get('wti',0):.1f} | DXY: {macro.get('dxy',0):.1f}\n"
        f"Gold: ${macro.get('gold',0):.0f}"
        + pm_lines
    )
    return _parse("Macro", _haiku(system, user))


def _risk_agent(cand: dict, portfolio_heat: float, pnl_today: float, trades_today: int) -> AgentOpinion:
    system = (
        "Sos un risk manager institucional. Evaluas si el perfil de riesgo "
        "del portfolio permite abrir la posicion propuesta. "
        "Respondé EXACTAMENTE: STANCE|CONFIDENCE|REASONING"
    )
    user = (
        f"Ticker: {cand['ticker']}\n"
        f"Notional: ${cand.get('notional',0):.0f} de $1400 budget\n"
        f"Stop loss: -1.5% (${cand.get('stop_loss',0):.2f})\n"
        f"Perdida maxima posible: ${cand.get('notional',0)*0.015:.0f}\n"
        f"Portfolio heat actual: {portfolio_heat:.1f}%\n"
        f"P&L del dia hasta ahora: ${pnl_today:+.0f}\n"
        f"Trades ejecutados hoy: {trades_today}\n"
        f"Regla: max 1 trade DT por dia"
    )
    return _parse("Risk", _haiku(system, user))


def _sentiment_agent(cand: dict, headlines: list[str], earnings_days: int | None) -> AgentOpinion:
    system = (
        "Sos un analista de sentiment e informacion cualitativa. "
        "Evaluas si el contexto noticioso y catalizadores apoyan el trade. "
        "Respondé EXACTAMENTE: STANCE|CONFIDENCE|REASONING"
    )
    hl = "\n".join(f"- {h}" for h in headlines[:4]) if headlines else "Sin noticias disponibles"
    earn = (
        f"ATENCION: EARNINGS EN {earnings_days} DIAS — riesgo gap adverso"
        if earnings_days and earnings_days <= 5
        else "Sin earnings proximos (< 5 dias)"
    )
    user = f"Ticker: {cand['ticker']}\n{earn}\nNoticias:\n{hl}"
    return _parse("Sentiment", _haiku(system, user))


# ── meta-agente ───────────────────────────────────────────────────────────────

def _meta_agent(cand: dict, opinions: list[AgentOpinion], direction: str) -> tuple[bool, float, str]:
    from anthropic import Anthropic
    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    go_count    = sum(1 for o in opinions if o.stance == "GO")
    reduce_count = sum(1 for o in opinions if o.stance == "REDUCE")
    avg_conf    = sum(o.confidence for o in opinions) / max(len(opinions), 1)
    ops_text    = "\n".join(
        f"  [{o.agent}] {o.stance} ({o.confidence}%): {o.reasoning}"
        for o in opinions
    )

    system = (
        "Sos el meta-agente coordinador de un sistema de trading orientado al crecimiento "
        "de capital en el mediano plazo. El objetivo es MULTIPLICAR el capital tomando riesgo "
        "productivo — no riesgo vacio, pero tampoco timidez que impide operar. "
        "El sistema ya filtro el candidato con criterios cuantitativos: si llego hasta aca, "
        "tiene un setup tecnico minimamente valido. Tu rol es calibrar el tamano, no vetar. "
        "Solo emitis NO-GO si hay una razon concreta de peso (earnings en 1-2 dias, "
        "macro en colapso, Risk veta por drawdown severo del dia). La duda beneficia al trade. "
        "Respondé EXACTAMENTE: DECISION|SIZE_FACTOR|REASONING "
        "(DECISION = GO o NO-GO, SIZE_FACTOR = 0.5 / 0.75 / 1.0, "
        "REASONING = 2-3 oraciones en espanol). Sin texto extra."
    )
    user = (
        f"TRADE PROPUESTO: {cand['ticker']} {direction}\n"
        f"Score cuantitativo: {cand.get('dt_score',0):.3f} | "
        f"Gap: {cand.get('gap_pct',0)*100:+.1f}% | ORB: {cand.get('orb_score',0):.2f}\n\n"
        f"OPINIONES DEL COMITE:\n{ops_text}\n\n"
        f"Resumen votos: GO={go_count}/4 | REDUCE={reduce_count}/4 | conf.avg={avg_conf:.0f}%\n\n"
        f"Regla de sizing (aplicar literalmente):\n"
        f"  2/4 GO -> GO con size 0.5 (probar el setup con mitad del capital)\n"
        f"  3/4 GO -> GO con size 0.75\n"
        f"  4/4 GO -> GO con size 1.0\n"
        f"  NO-GO solo si: earnings inminentes (1-2 dias) O Risk reporta drawdown severo hoy."
    )

    try:
        text = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            system=system,
            messages=[{"role": "user", "content": user}],
        ).content[0].text.strip()

        parts   = text.split("|", 2)
        decision = parts[0].strip().upper()
        size_f   = float(parts[1].strip()) if len(parts) > 1 else 1.0
        reason   = parts[2].strip() if len(parts) > 2 else text
        go = (decision == "GO")

        # Veto de Risk: si Risk dice NO-GO, forzamos size reducido o NO-GO
        risk_op = next((o for o in opinions if o.agent == "Risk"), None)
        if risk_op and risk_op.stance == "NO-GO" and go:
            size_f = min(size_f, 0.5)
            reason += " (size reducido por veto de Risk)"

        return go, max(0.0, min(1.0, size_f)), reason

    except Exception as e:
        log.error("Meta-agente fallo: %s", e)
        # Fallback: 2/4 GO ya alcanza para ejecutar (growth mode)
        go = go_count >= 2
        size_f = 1.0 if go_count == 4 else 0.75 if go_count == 3 else 0.5
        return go, size_f, f"Fallback mayoría: {go_count}/4 GO"


# ── API publica ───────────────────────────────────────────────────────────────

def evaluate(
    candidate: dict,
    direction: str = "LONG",
    macro_ctx: dict | None = None,
    portfolio_heat: float = 0.0,
    pnl_today: float = 0.0,
    trades_today: int = 0,
    headlines: list[str] | None = None,
    earnings_days: int | None = None,
    polymarket: dict | None = None,
) -> CommitteeDecision:
    """
    Corre los 4 agentes en paralelo → meta-agente en serie.

    Args:
        candidate:      dict del candidato DT (output de scan_dt_candidates)
        direction:      "LONG" o "SHORT"
        macro_ctx:      dict con regime, vix, wti, dxy, spy_vwap_dev
        portfolio_heat: % del capital en riesgo en posiciones actuales
        pnl_today:      P&L realizado del dia (en $)
        trades_today:   trades DT ejecutados hoy
        headlines:      titulares recientes del ticker
        earnings_days:  dias hasta el proximo earnings (None si no se sabe)
        polymarket:     dict de señales Polymarket {key: probability}

    Returns:
        CommitteeDecision con go, size_factor, reasoning, opinions
    """
    macro_ctx = macro_ctx or {}

    log.info("Committee evaluando %s %s...", candidate.get("ticker", "?"), direction)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        f_tech = ex.submit(_technical_agent, candidate, direction)
        f_macro = ex.submit(_macro_agent, candidate, macro_ctx, polymarket)
        f_risk  = ex.submit(_risk_agent, candidate, portfolio_heat, pnl_today, trades_today)
        f_sent  = ex.submit(_sentiment_agent, candidate, headlines or [], earnings_days)

        opinions: list[AgentOpinion] = []
        for fut, name in [(f_tech,"Technical"),(f_macro,"Macro"),(f_risk,"Risk"),(f_sent,"Sentiment")]:
            try:
                opinions.append(fut.result(timeout=30))
            except Exception as e:
                log.warning("Agente %s timeout/error: %s", name, e)
                opinions.append(AgentOpinion(agent=name, stance="NO-GO", confidence=0, reasoning=str(e)[:100]))

    for o in opinions:
        log.info("  [%s] %s (%d%%) — %s", o.agent, o.stance, o.confidence, o.reasoning[:80])

    go, size_f, reason = _meta_agent(candidate, opinions, direction)
    go_count = sum(1 for o in opinions if o.stance == "GO")

    log.info(
        "Committee FINAL %s: %s | size=%.1f | %s",
        candidate.get("ticker","?"), "GO" if go else "NO-GO", size_f, reason[:100],
    )

    return CommitteeDecision(
        go=go, size_factor=size_f, reasoning=reason,
        opinions=opinions, go_count=go_count,
    )
