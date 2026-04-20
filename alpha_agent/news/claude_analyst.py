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

_MODEL = "claude-haiku-4-5-20251001"


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


# ── 3. Earnings / Event Impact Quick Score ────────────────────────────────────

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
