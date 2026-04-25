"""
Claude-powered intelligence for position decisions and market context.

Two uses:
  1. assess_position() — called by the monitor when a position is near its stop.
     Returns CLOSE / HOLD / REDUCE with a plain-language reason.
  2. summarize_market_context() — called by the analyst to enrich the WhatsApp
     brief with a 2-sentence macro narrative.

Uses claude-haiku-4-5 (fast, cheap, ~$0.001 per full analyst run).
Fails silently if ANTHROPIC_API_KEY is missing or the call errors out.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Literal

logger = logging.getLogger(__name__)

_MODEL_FAST = "claude-haiku-4-5-20251001"   # real-time decisions, classification
_MODEL_DEEP = "claude-sonnet-4-6"           # investment thesis, SEC analysis
_MODEL = _MODEL_FAST  # default for backwards compat


def _client():
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import anthropic
        return anthropic.Anthropic(api_key=api_key)
    except ImportError:
        logger.debug("anthropic SDK not installed — Claude features disabled.")
        return None


# ── 1. Position Assessment ────────────────────────────────────────────────────

PositionAction = Literal["CLOSE", "HOLD", "REDUCE"]


def assess_position(
    ticker: str,
    current_price: float,
    entry_price: float,
    pnl_pct: float,
    stop_loss: float | None,
    news_headlines: list[str],
    macro_regime: str,
) -> dict | None:
    """
    Ask Claude whether to CLOSE, HOLD, or REDUCE a position.

    Returns a dict: {"action": str, "confidence": float, "reason": str}
    or None if Claude is unavailable or the call fails.

    Only called when a position is within 1.5% of its stop — so the cost
    is essentially zero on normal days.
    """
    client = _client()
    if client is None:
        return None

    news_block = (
        "\n".join(f"- {h}" for h in news_headlines[:6])
        if news_headlines
        else "No recent news available."
    )
    sl_text = f"${stop_loss:.2f}" if stop_loss else "N/A (dynamic trailing)"

    prompt = f"""You are a systematic risk manager at a quant fund. Evaluate this open equity position.

POSITION
  Ticker: {ticker}
  Entry: ${entry_price:.2f}  |  Current: ${current_price:.2f}  |  P&L: {pnl_pct:+.1f}%
  Hard stop: {sl_text}
  Market regime: {macro_regime.upper()}

RECENT NEWS FOR {ticker}
{news_block}

Decide: should we CLOSE, HOLD, or REDUCE (sell half)?

Rules:
- CLOSE: news shows fundamental deterioration, fraud, sector systemic shock, or geopolitical event directly harming this name.
- REDUCE: mixed signals — some negative news but position thesis still partly valid.
- HOLD: normal volatility, no new negative catalysts, thesis intact.
- If P&L > -1% and no bad news → always HOLD.
- Ignore noise; focus on material events that change the 3-6 month outlook.

Respond with JSON only, no prose:
{{"action": "CLOSE"|"HOLD"|"REDUCE", "confidence": 0.0-1.0, "reason": "≤15 words"}}"""

    try:
        msg = client.messages.create(
            model=_MODEL,
            max_tokens=80,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        match = re.search(r"\{.*?\}", text, re.DOTALL)
        if not match:
            return None
        result = json.loads(match.group())
        action = result.get("action", "HOLD")
        if action not in ("CLOSE", "HOLD", "REDUCE"):
            action = "HOLD"
        return {
            "action": action,
            "confidence": float(result.get("confidence", 0.5)),
            "reason": str(result.get("reason", "")),
        }
    except Exception as exc:
        logger.debug("Claude position assess failed (%s)", exc)
        return None


# ── 2. Market Context Narrative ───────────────────────────────────────────────

def build_macro_narrative(
    macro: dict,
    top_signals: list[dict],
    radar_entries: list[dict],
) -> str | None:
    """
    Generate a 2-sentence market narrative for the WhatsApp brief.
    Returns None if Claude unavailable — caller falls back to deterministic text.
    """
    client = _client()
    if client is None:
        return None

    regime = macro.get("regime", "unknown").upper()
    vix = macro.get("prices", {}).get("vix", "?")
    wti = macro.get("prices", {}).get("oil_wti", "?")
    gold = macro.get("prices", {}).get("gold", "?")

    tickers_picked = [s.get("ticker") for s in top_signals[:4]]
    top_movers = [
        f"{e.get('ticker')} {e.get('move_pct', 0):+.1f}%"
        for e in radar_entries[:5]
        if e.get("move_pct") is not None
    ]

    prompt = f"""You are a concise financial analyst. Write EXACTLY 2 sentences summarizing today's market for a Latin American investor.

Data:
- Regime: {regime}
- VIX: {vix} | WTI: ${wti} | Gold: ${gold}
- Portfolio picks today: {', '.join(tickers_picked) if tickers_picked else 'none'}
- Top movers in universe: {', '.join(top_movers) if top_movers else 'none'}

Tone: direct, no fluff. Mention the most important risk or opportunity.
Write in Spanish. MAX 40 words total. No emojis."""

    try:
        msg = client.messages.create(
            model=_MODEL,
            max_tokens=80,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as exc:
        logger.debug("Claude macro narrative failed (%s)", exc)
        return None


# ── 3. Wall Street Analyst — Full Fundamental + Quant Analysis ───────────────

def wall_street_analysis(
    ticker: str,
    fundamentals: dict,
    quant: dict,
    news_headlines: list[str],
    macro_regime: str,
    *,
    sector: str = "Other",
) -> dict | None:
    """
    Genera una tesis de inversión completa tipo analista senior de Wall Street.

    Args:
        ticker: símbolo del activo
        fundamentals: dict de get_fundamentals() — P/E, FCF yield, ROE, etc.
        quant: dict con beta, alpha_jensen, sharpe, rsi, ret_1m, ret_3m
        news_headlines: lista de headlines recientes del ticker
        macro_regime: 'bull' | 'bear' | 'lateral'
        sector: sector del activo

    Returns:
        dict con keys: thesis, catalysts, risks, valuation, recommendation, price_target_pct
        o None si Claude no está disponible.
    """
    client = _client()
    if client is None:
        return None

    from alpha_agent.analytics.fundamental import format_for_claude

    fundamental_block = format_for_claude(ticker, fundamentals, quant=quant)

    news_block = (
        "\n".join(f"- {h}" for h in news_headlines[:5])
        if news_headlines
        else "No hay noticias recientes disponibles."
    )

    prompt = f"""Sos un analista senior de renta variable en una gestora de primer nivel de Wall Street.
Analizá {ticker} con la siguiente información y generá una tesis de inversión completa.

{fundamental_block}

NOTICIAS RECIENTES:
{news_block}

CONTEXTO MACRO: Régimen {macro_regime.upper()}

Generá un análisis estructurado. Respondé con JSON válido únicamente:
{{
  "thesis": "2-3 líneas: por qué comprar/vender/mantener ahora",
  "catalysts": "próximos 1-3 catalizadores concretos que podrían mover el precio",
  "risks": "1-2 riesgos específicos y cuantificados si es posible",
  "valuation": "CARO|JUSTO|BARATO — 1 línea de justificación vs sector/histórico",
  "recommendation": "BUY|HOLD|SELL",
  "price_target_pct": 12.5
}}

price_target_pct es el upside/downside esperado en % a 12 meses (puede ser negativo).
Sé concreto y directo. No uses frases vagas."""

    try:
        # Sonnet para análisis profundo — calidad notablemente superior a Haiku
        # para tesis de inversión. Costo ~$0.006/ticker, ~7 tickers/día ≈ $0.04/día.
        msg = client.messages.create(
            model=_MODEL_DEEP,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        result = json.loads(match.group())
        if result.get("recommendation") not in ("BUY", "HOLD", "SELL"):
            result["recommendation"] = "HOLD"
        return result
    except Exception as exc:
        logger.debug("Claude wall_street_analysis failed (%s)", exc)
        return None


# ── 4. Risk Arbiter — debate bull/bear antes de ejecutar ─────────────────────

def risk_debate(
    ticker: str,
    signal: dict,
    portfolio_context: dict,
) -> dict:
    """
    Debate estructurado bull/bear para validar una señal antes del trader.

    Inspirado en TradingAgents (github.com/tauricresearch/tradingagents).
    Usado en el rebalancer semanal para filtrar señales antes de ejecutarlas.

    Args:
        ticker: símbolo
        signal: dict con price, stop_loss, take_profit, thesis, conviction, etc.
        portfolio_context: dict con regime, vix, current_positions, capital_usd

    Returns:
        dict con: bull_case, bear_case, verdict (PROCEED|REDUCE_SIZE|SKIP),
                  confidence, size_adjustment (multiplicador 0.5-1.0)
    """
    client = _client()

    # Default: si Claude no está disponible, siempre proceder
    default = {
        "bull_case": "Claude no disponible — usando señal original",
        "bear_case": "Sin análisis",
        "verdict": "PROCEED",
        "confidence": 0.5,
        "size_adjustment": 1.0,
    }

    if client is None:
        return default

    regime    = portfolio_context.get("regime", "unknown")
    vix       = portfolio_context.get("vix", 20)
    positions = portfolio_context.get("current_positions", [])
    capital   = portfolio_context.get("capital_usd", 1600)
    conv      = signal.get("thesis", {}).get("conviction", "MEDIA")
    sharpe    = signal.get("thesis", {}).get("quant", {}).get("sharpe", 0) or 0
    alpha     = (signal.get("thesis", {}).get("quant", {}).get("alpha_jensen", 0) or 0) * 100
    stop      = signal.get("stop_loss")
    tp        = signal.get("take_profit")
    price     = signal.get("price", 0)
    r_r       = ((tp - price) / (price - stop)) if (tp and stop and price and price > stop) else None

    prompt = f"""Sos un risk arbitrage committee evaluando si ejecutar esta señal.

SEÑAL: {ticker}
- Conviction: {conv} | Sharpe: {sharpe:.2f} | Alpha Jensen: {alpha:+.1f}%
- Precio: ${price:.2f} | Stop: ${f'${stop:.2f}' if stop else 'N/D'} | TP: ${f'${tp:.2f}' if tp else 'N/D'}
- R/R implícito: {f'{r_r:.1f}x' if r_r else 'N/D'}

CONTEXTO: Régimen {regime.upper()} | VIX {vix:.1f} | Capital ${capital:.0f}
Posiciones actuales: {', '.join(positions) if positions else 'ninguna'}

Generá el debate y el veredicto en JSON (solo JSON):
{{
  "bull_case": "argumento más fuerte A FAVOR en 1-2 líneas",
  "bear_case": "argumento más fuerte EN CONTRA en 1-2 líneas",
  "verdict": "PROCEED|REDUCE_SIZE|SKIP",
  "confidence": 0.0-1.0,
  "size_adjustment": 1.0
}}

Guías:
- PROCEED: señal válida, ejecutar con tamaño normal (size_adjustment=1.0)
- REDUCE_SIZE: señal válida pero contexto adverso (size_adjustment=0.5-0.75)
- SKIP: señal inválida o riesgo asimétrico (size_adjustment=0.0)
- VIX > 25 con régimen BEAR → size_adjustment máximo 0.75"""

    try:
        msg = client.messages.create(
            model=_MODEL,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return default
        result = json.loads(match.group())
        if result.get("verdict") not in ("PROCEED", "REDUCE_SIZE", "SKIP"):
            result["verdict"] = "PROCEED"
        result["size_adjustment"] = max(0.0, min(1.0, float(result.get("size_adjustment", 1.0))))
        return result
    except Exception as exc:
        logger.debug("Claude risk_debate failed (%s)", exc)
        return default


# ── 5. Earnings / Event Impact Quick Score ────────────────────────────────────

def score_event_impact(ticker: str, headline: str, sector: str) -> int:
    """
    Fast classification of a single headline: +1 (bullish), 0 (neutral), -1 (bearish).
    Uses Claude only when ANTHROPIC_API_KEY is set; returns 0 otherwise.
    """
    client = _client()
    if client is None:
        return 0
    try:
        msg = client.messages.create(
            model=_MODEL,
            max_tokens=5,
            messages=[{
                "role": "user",
                "content": (
                    f"Financial news: \"{headline}\"\n"
                    f"Stock: {ticker} (sector: {sector})\n"
                    "Impact on stock price next week? Reply ONLY with: 1, 0, or -1"
                ),
            }],
        )
        text = msg.content[0].text.strip()
        val = int(re.search(r"-?1|0", text).group())
        return max(-1, min(1, val))
    except Exception:
        return 0
