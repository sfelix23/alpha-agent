"""
AI-driven allocation decision engine.

Each morning, instead of hardcoded sleeve percentages, Claude Haiku reads the
macro context and recent performance history and decides how to split capital
between CP momentum, LP equity, and cash. This replaces static config rules.

Decision output example (BULL, VIX 16):
  lp_pct=0.0, cp_pct=0.90, n_cp_positions=2, cp_max_hold_days=3

Falls back to rule-based defaults if the API is unavailable.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class AllocationDecision:
    lp_pct: float          # fraction of capital in LP equity (0-1)
    cp_pct: float          # fraction of capital in CP momentum (0-1)
    opt_pct: float         # fraction in options (always 0 for now)
    n_cp_positions: int    # how many concentrated CP positions (1-3)
    cp_max_hold_days: int  # max days to hold a CP before forced exit
    reasoning: str         # one-sentence rationale


# Rule-based fallbacks (used when API unavailable)
_BULL_DEFAULT    = AllocationDecision(0.0, 0.98, 0.0, 2, 3, "BULL: capital totalmente desplegado en CP momentum.")
_NEUTRAL_DEFAULT = AllocationDecision(0.0, 0.80, 0.0, 2, 3, "NEUTRAL: CP alta exposición con buffer mínimo.")
_BEAR_DEFAULT    = AllocationDecision(0.0, 0.45, 0.0, 1, 2, "BEAR: postura defensiva, CP mínimo.")


def _rule_default(regime: str, vix: float) -> AllocationDecision:
    if vix > 30 or regime.upper() == "BEAR":
        return _BEAR_DEFAULT
    if vix > 22 or regime.upper() == "NEUTRAL":
        return _NEUTRAL_DEFAULT
    return _BULL_DEFAULT


def _get_recent_performance() -> tuple[float | None, float | None]:
    """Reads recent win_rate and total P&L from trade_db (last 7 days)."""
    try:
        from alpha_agent.analytics.trade_db import get_trades
        from datetime import datetime, timedelta
        cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        trades = get_trades(limit=100)
        recent = [t for t in trades if (t.get("date") or "") >= cutoff and t.get("pnl_usd") is not None]
        if not recent:
            return None, None
        wins = sum(1 for t in recent if (t.get("pnl_usd") or 0) > 0)
        total_pnl = sum((t.get("pnl_usd") or 0) for t in recent)
        win_rate = wins / len(recent) if recent else None
        return round(win_rate, 2), round(total_pnl, 2)
    except Exception as exc:
        log.debug("get_recent_performance: %s", exc)
        return None, None


def decide_allocation(
    regime: str,
    vix: float,
    capital: float = 1600.0,
    sector_momentum: dict[str, float] | None = None,
) -> AllocationDecision:
    """
    Uses Claude Haiku to decide the optimal capital allocation for today.
    Fetches recent performance from trade_db automatically.
    Falls back to rule-based defaults if Anthropic API is unavailable.
    """
    win_rate, recent_pnl = _get_recent_performance()

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        log.warning("ANTHROPIC_API_KEY not set — using rule-based allocation")
        return _rule_default(regime, vix)

    ctx: dict[str, Any] = {
        "regime": regime,
        "vix": round(vix, 1),
        "capital_usd": capital,
    }
    if win_rate is not None:
        ctx["recent_win_rate_7d"] = win_rate
    if recent_pnl is not None:
        ctx["recent_pnl_7d_usd"] = recent_pnl
    if sector_momentum:
        top = sorted(sector_momentum.items(), key=lambda x: x[1], reverse=True)[:3]
        ctx["top_sectors_momentum"] = {k: round(v, 3) for k, v in top}

    prompt = (
        "You are the allocation AI for a $1600 momentum trading account.\n"
        "Decide today's optimal capital split based on this market context:\n\n"
        f"{json.dumps(ctx, indent=2)}\n\n"
        "Rules:\n"
        "- LP sleeve is OFF (capital too small for long-term holds)\n"
        "- CP momentum trades (1-5 days): 35-90% of capital\n"
        "- Options: always 0% (too costly for this capital)\n"
        "- cash = 1 - cp_pct (protective buffer)\n"
        "- n_cp_positions: 1 (very concentrated) or 2 (two bets)\n"
        "- cp_max_hold_days: 2 (BEAR/high VIX) to 4 (strong BULL)\n"
        "- In BULL + VIX<18: use cp_pct=0.98 (near-full deployment), 2 positions, 3 days max\n"
        "- In BULL + VIX 18-25: use cp_pct=0.85-0.92, 2 positions, 3 days max\n"
        "- In NEUTRAL or VIX>25: use cp_pct=0.70-0.80, 2 positions, 3 days max\n"
        "- In BEAR or VIX>30: use cp_pct=0.40-0.50, 1 position, 2 days max\n"
        "- recent_pnl_7d and win_rate should adjust cp_pct: losing streak → reduce 5-10%\n\n"
        "Respond ONLY with a JSON object (no markdown, no explanation):\n"
        '{"lp_pct":0.0,"cp_pct":<0.40-0.98>,"opt_pct":0.0,'
        '"n_cp_positions":<1 or 2>,"cp_max_hold_days":<2-4>,'
        '"reasoning":"<una frase en español>"}'
    )

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        # Strip markdown fences if present
        for fence in ("```json", "```"):
            if raw.startswith(fence):
                raw = raw[len(fence):]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()

        data = json.loads(raw)
        dec = AllocationDecision(
            lp_pct=float(data.get("lp_pct", 0.0)),
            cp_pct=min(0.98, max(0.40, float(data.get("cp_pct", 0.80)))),
            opt_pct=0.0,
            n_cp_positions=max(1, min(3, int(data.get("n_cp_positions", 2)))),
            cp_max_hold_days=max(2, min(5, int(data.get("cp_max_hold_days", 3)))),
            reasoning=str(data.get("reasoning", "AI allocation applied.")),
        )
        log.info(
            "AI Allocation → LP=%.0f%% CP=%.0f%% cash=%.0f%% | %d pos | max %dd | %s",
            dec.lp_pct * 100, dec.cp_pct * 100,
            (1.0 - dec.lp_pct - dec.cp_pct) * 100,
            dec.n_cp_positions, dec.cp_max_hold_days, dec.reasoning,
        )
        return dec

    except Exception as exc:
        log.warning("AI allocation failed (%s) — rule-based fallback", exc)
        return _rule_default(regime, vix)
