"""
Swarm simplificado para validación de setups de scalping.

Solo 2 agentes (Haiku) para minimizar latencia:
  ScalpTechnical → evalúa el setup ORB
  ScalpRisk      → evalúa R/R y contexto de portfolio

Latencia target: < 3 segundos
Costo: ~$0.002 por validación
"""

from __future__ import annotations

import concurrent.futures
import logging
import os

log = logging.getLogger(__name__)


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


def _parse(text: str) -> tuple[bool, str]:
    """Extrae GO/NO-GO del formato 'GO|REASONING' o 'NO-GO|REASONING'."""
    try:
        parts = text.split("|", 1)
        go = parts[0].strip().upper() == "GO"
        reason = parts[1].strip() if len(parts) > 1 else text[:150]
        return go, reason
    except Exception:
        return False, text[:150]


def validate_scalp(
    ticker: str,
    direction: str,
    bracket: dict,
    orb_range_pct: float,
    trades_today: int,
    vix: float,
) -> tuple[bool, str]:
    """
    Validación rápida de un setup de scalping. Solo 2 agentes en paralelo.

    Returns:
        (go: bool, reasoning: str)
    """
    sys_tech = (
        "Sos un scalper institucional. Evaluás si un breakout ORB es válido para scalping. "
        "Respondé EXACTAMENTE: GO|REASONING o NO-GO|REASONING (1 oración). Sin texto extra."
    )
    user_tech = (
        f"Ticker: {ticker} | Dirección: {direction}\n"
        f"Rango ORB: {orb_range_pct:.2f}% | SL: {bracket['sl_pct']:.2f}% | "
        f"TP: {bracket['tp_pct']:.2f}% | R/R: {bracket['rr']:.2f}:1\n"
        f"Notional: ${bracket['notional']:.0f} | Qty: {bracket['qty']} acciones"
    )

    sys_risk = (
        "Sos el Risk Manager de un scalper. Evaluás si el contexto permite abrir una posición adicional. "
        "Respondé EXACTAMENTE: GO|REASONING o NO-GO|REASONING (1 oración). Sin texto extra."
    )
    user_risk = (
        f"Trades hoy: {trades_today}/4 | VIX: {vix:.1f}\n"
        f"R/R del trade: {bracket['rr']:.2f}:1 | SL: {bracket['sl_pct']:.2f}%\n"
        f"VIX > 30 = NO-GO siempre | trades >= 4 = NO-GO"
    )

    go_tech = go_risk = True
    reason_tech = reason_risk = ""

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(_haiku, sys_tech, user_tech)
        f2 = ex.submit(_haiku, sys_risk, user_risk)
        try:
            go_tech, reason_tech = _parse(f1.result(timeout=15))
        except Exception as e:
            reason_tech = str(e)[:80]
        try:
            go_risk, reason_risk = _parse(f2.result(timeout=15))
        except Exception as e:
            reason_risk = str(e)[:80]

    go     = go_tech and go_risk
    reason = f"Tech: {reason_tech} | Risk: {reason_risk}"
    log.info("ScalpSwarm %s %s: %s — %s", ticker, direction, "GO" if go else "NO-GO", reason[:100])
    return go, reason
