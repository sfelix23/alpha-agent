"""
Swarm Orchestrator — protocolo de debate adversarial en 2 rondas.

Ronda 1 (paralela):   Strategist + TechnicalAnalyst + SentimentAgent
Ronda 2 (secuencial): RiskAuditor lee el output de TechnicalAnalyst y lo refuta
Ronda 3:              Meta-agente (Sonnet) sintetiza el debate completo

El debate completo se persiste en signals/swarm_debates.json
para que el dashboard pueda mostrarlo en tiempo real.

Costo total por decision: ~$0.012 (Haiku x4 CoT + Sonnet x1)
Latencia: ~6-10 segundos
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from alpha_agent.swarm.agents import (
    SwarmOpinion,
    risk_auditor,
    sentiment_agent,
    strategist_agent,
    technical_analyst,
)
from alpha_agent.swarm.ev_calculator import compute_ev

log = logging.getLogger(__name__)

_DEBATE_LOG_PATH = Path(__file__).resolve().parents[3] / "signals" / "swarm_debates.json"
_MAX_DEBATES     = 30


@dataclass
class SwarmDecision:
    go:          bool
    size_factor: float                        # 0.5 / 0.75 / 1.0
    reasoning:   str                          # explicación del meta-agente
    opinions:    list[SwarmOpinion] = field(default_factory=list)
    go_count:    int = 0
    ev_data:     dict = field(default_factory=dict)
    debate_id:   str = ""


# ── debate log ────────────────────────────────────────────────────────────────

def _save_debate(debate: dict) -> None:
    try:
        _DEBATE_LOG_PATH.parent.mkdir(exist_ok=True)
        existing: list = []
        if _DEBATE_LOG_PATH.exists():
            try:
                existing = json.loads(_DEBATE_LOG_PATH.read_text(encoding="utf-8"))
            except Exception:
                existing = []
        existing.insert(0, debate)
        existing = existing[:_MAX_DEBATES]
        _DEBATE_LOG_PATH.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        log.debug("debate_log write error: %s", e)


def load_debates(limit: int = 10) -> list[dict]:
    try:
        if _DEBATE_LOG_PATH.exists():
            data = json.loads(_DEBATE_LOG_PATH.read_text(encoding="utf-8"))
            return data[:limit] if isinstance(data, list) else []
    except Exception:
        pass
    return []


# ── meta-agente (Sonnet) ──────────────────────────────────────────────────────

def _meta_agent(
    cand: dict,
    opinions: list[SwarmOpinion],
    direction: str,
    ev_data: dict,
) -> tuple[bool, float, str]:
    from anthropic import Anthropic
    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    go_count     = sum(1 for o in opinions if o.stance == "GO")
    reduce_count = sum(1 for o in opinions if o.stance == "REDUCE")
    nogo_count   = sum(1 for o in opinions if o.stance == "NO-GO")
    avg_conf     = sum(o.confidence for o in opinions) / max(len(opinions), 1)

    ops_text = "\n".join(
        f"  [{o.agent}] {o.stance} ({o.confidence}%) — {o.reasoning}"
        + (f"\n    CoT:\n" + "\n".join(f"      {ln}" for ln in o.chain_of_thought.splitlines())
           if o.chain_of_thought else "")
        for o in opinions
    )

    ev_summary = (
        f"Expected Value: ${ev_data.get('ev', 0):+.2f} "
        f"({'POSITIVO ✓' if ev_data.get('ev_positive') else 'NEGATIVO ✗'})\n"
        f"Kelly sizing: {ev_data.get('kelly_fraction', 0)*100:.1f}% del capital | "
        f"Fuente: {ev_data.get('source', 'N/A')}"
    ) if ev_data else ""

    system = (
        "Sos el Orquestador de un Swarm de trading de alta convicción orientado al "
        "crecimiento de capital. Cuatro especialistas debatieron el trade. "
        "Tu rol es sintetizar el debate y emitir la decisión final con sizing preciso.\n\n"
        "Filosofía de sizing:\n"
        "  - EV positivo + 3-4 GO → size 1.0\n"
        "  - EV positivo + 2-3 GO → size 0.75\n"
        "  - EV positivo + 2 GO con Risk REDUCE → size 0.5\n"
        "  - EV NEGATIVO → NO-GO (regla dura, sin excepciones)\n"
        "  - RiskAuditor NO-GO con argumento sólido → size 0.5 o NO-GO\n"
        "  - Earnings en 1-2 días → NO-GO siempre\n\n"
        "La duda matemática (EV positivo) beneficia al trade. "
        "NO-GO se reserva para fallas concretas, no para incertidumbre general.\n\n"
        "Respondé EXACTAMENTE: DECISION|SIZE_FACTOR|REASONING\n"
        "(DECISION = GO o NO-GO, SIZE_FACTOR = 0.5 / 0.75 / 1.0, "
        "REASONING = 2-3 oraciones en español). Sin texto extra."
    )

    user = (
        f"TRADE: {cand['ticker']} {direction} | Score: {cand.get('dt_score', 0):.3f} | "
        f"Gap: {cand.get('gap_pct', 0)*100:+.1f}% | ORB: {cand.get('orb_score', 0):.2f}\n\n"
        f"DEBATE DEL SWARM:\n{ops_text}\n\n"
        f"RESUMEN: GO={go_count} | REDUCE={reduce_count} | NO-GO={nogo_count} | "
        f"conf.avg={avg_conf:.0f}%\n\n"
        f"{ev_summary}"
    )

    try:
        text = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            system=system,
            messages=[{"role": "user", "content": user}],
        ).content[0].text.strip()

        parts    = text.split("|", 2)
        decision = parts[0].strip().upper()
        size_f   = float(parts[1].strip()) if len(parts) > 1 else 1.0
        reason   = parts[2].strip() if len(parts) > 2 else text
        go = (decision == "GO")

        # Veto duro: EV negativo → NO-GO siempre
        if ev_data.get("ev_positive") is False and ev_data:
            go     = False
            reason = f"EV negativo (${ev_data.get('ev', 0):.2f}) — trade matemáticamente inválido. " + reason

        # Veto parcial: RiskAuditor NO-GO → cap en 0.5
        risk_op = next((o for o in opinions if o.agent == "RiskAuditor"), None)
        if risk_op and risk_op.stance == "NO-GO" and go:
            size_f = min(size_f, 0.5)
            reason += " (size recortado a 0.5 por veto de RiskAuditor)"

        return go, max(0.0, min(1.0, size_f)), reason

    except Exception as e:
        log.error("Meta-agente falló: %s", e)
        go_count_local = sum(1 for o in opinions if o.stance == "GO")
        ev_ok = ev_data.get("ev_positive", True) if ev_data else True
        go     = go_count_local >= 2 and ev_ok
        size_f = 1.0 if go_count_local == 4 else 0.75 if go_count_local == 3 else 0.5
        return go, size_f, f"Fallback mayoría: {go_count_local}/4 GO, EV {'✓' if ev_ok else '✗'}"


# ── API pública ───────────────────────────────────────────────────────────────

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
) -> SwarmDecision:
    """
    Protocolo de debate adversarial en 2 rondas.

    Ronda 1 (paralela):   Strategist + Technical + Sentiment
    Ronda 2 (secuencial): RiskAuditor con contexto del Technical
    Ronda 3:              Meta-agente sintetiza todo

    Returns SwarmDecision con go, size_factor, reasoning, opinions, ev_data
    """
    macro_ctx = macro_ctx or {}
    ticker    = candidate.get("ticker", "?")
    debate_id = f"{ticker}-{direction}-{int(time.time())}"

    log.info("Swarm debatiendo %s %s [%s]...", ticker, direction, debate_id)

    # ── EV pre-cálculo (no depende de los agentes) ────────────────────────────
    sl_pct  = abs(candidate.get("current_price", 1) - candidate.get("stop_loss", 0.985)) / max(candidate.get("current_price", 1), 0.01)
    tp1_pct = 0.03
    tp2_pct = 0.07
    try:
        px = candidate.get("current_price", 1) or 1
        sl = candidate.get("stop_loss",     px * 0.985)
        tp1 = candidate.get("take_profit_1", px * 1.03)
        tp2 = candidate.get("take_profit_2", px * 1.07)
        if direction == "SHORT":
            sl_pct  = abs(sl - px) / px
            tp1_pct = abs(px - tp1) / px
            tp2_pct = abs(px - tp2) / px
        else:
            sl_pct  = abs(px - sl)  / px
            tp1_pct = abs(tp1 - px) / px
            tp2_pct = abs(tp2 - px) / px
    except Exception:
        pass

    ev_data = compute_ev(
        notional=candidate.get("notional", 1400.0),
        sl_pct=sl_pct,
        tp1_pct=tp1_pct,
        tp2_pct=tp2_pct,
    )
    log.info("EV[%s]: $%+.2f (win_rate=%.0f%%, kelly=%.1f%%)",
             ticker, ev_data["ev"], ev_data["win_rate"]*100, ev_data["kelly_fraction"]*100)

    # ── Ronda 1: Strategist + Technical + Sentiment en paralelo ──────────────
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        f_strat = ex.submit(strategist_agent, macro_ctx, polymarket, direction)
        f_tech  = ex.submit(technical_analyst, candidate, direction)
        f_sent  = ex.submit(sentiment_agent, candidate, headlines or [], earnings_days)

        strat_op = tech_op = sent_op = None
        for fut, name in [(f_strat, "Strategist"), (f_tech, "Technical"), (f_sent, "Sentiment")]:
            try:
                op = fut.result(timeout=35)
                if name == "Strategist": strat_op = op
                elif name == "Technical": tech_op = op
                else: sent_op = op
                log.info("  [%s] %s (%d%%) — %s", op.agent, op.stance, op.confidence, op.reasoning[:80])
            except Exception as e:
                log.warning("Ronda1 %s timeout/error: %s", name, e)
                from alpha_agent.swarm.agents import SwarmOpinion
                dummy = SwarmOpinion(agent=name, chain_of_thought="", stance="NO-GO",
                                     confidence=0, reasoning=str(e)[:100])
                if name == "Strategist": strat_op = dummy
                elif name == "Technical": tech_op = dummy
                else: sent_op = dummy

    # ── Ronda 2: RiskAuditor lee al Technical y lo refuta ────────────────────
    try:
        risk_op = risk_auditor(
            cand=candidate,
            technical_opinion=tech_op,
            portfolio_heat=portfolio_heat,
            pnl_today=pnl_today,
            trades_today=trades_today,
            ev_data=ev_data,
        )
        log.info("  [RiskAuditor] %s (%d%%) EV=$%+.2f — %s",
                 risk_op.stance, risk_op.confidence, ev_data["ev"], risk_op.reasoning[:80])
    except Exception as e:
        log.warning("RiskAuditor error: %s", e)
        from alpha_agent.swarm.agents import SwarmOpinion
        risk_op = SwarmOpinion(agent="RiskAuditor", chain_of_thought="", stance="NO-GO",
                                confidence=0, reasoning=str(e)[:100], ev=ev_data.get("ev"))

    opinions = [strat_op, tech_op, sent_op, risk_op]

    # ── Ronda 3: Meta-agente sintetiza ────────────────────────────────────────
    go, size_f, reason = _meta_agent(candidate, opinions, direction, ev_data)
    go_count = sum(1 for o in opinions if o.stance == "GO")

    log.info(
        "Swarm FINAL %s: %s | size=%.2f | GO=%d/4 | EV=$%+.2f | %s",
        ticker, "GO" if go else "NO-GO", size_f, go_count, ev_data["ev"], reason[:100],
    )

    # ── Persistir debate ──────────────────────────────────────────────────────
    def _op_to_dict(o: SwarmOpinion) -> dict:
        d = {
            "agent": o.agent,
            "chain_of_thought": o.chain_of_thought,
            "stance": o.stance,
            "confidence": o.confidence,
            "reasoning": o.reasoning,
        }
        if o.ev is not None:
            d["ev"] = o.ev
        if o.ev_data:
            d["ev_data"] = o.ev_data
        return d

    debate_record = {
        "id":        debate_id,
        "ts":        time.strftime("%Y-%m-%dT%H:%M:%S"),
        "ticker":    ticker,
        "direction": direction,
        "dt_score":  candidate.get("dt_score", 0),
        "gap_pct":   round(candidate.get("gap_pct", 0) * 100, 2),
        "opinions":  [_op_to_dict(o) for o in opinions],
        "go_count":  go_count,
        "ev_data":   ev_data,
        "decision":  {"go": go, "size_factor": size_f, "reasoning": reason},
    }
    _save_debate(debate_record)

    return SwarmDecision(
        go=go,
        size_factor=size_f,
        reasoning=reason,
        opinions=opinions,
        go_count=go_count,
        ev_data=ev_data,
        debate_id=debate_id,
    )
