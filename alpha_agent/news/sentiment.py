"""
Sentiment scoring — tres capas en cascada:

  1. Claude claude-haiku-4-5  (primario — si ANTHROPIC_API_KEY está disponible)
  2. Gemini Flash       (secundario — si GOOGLE_API_KEY está disponible)
  3. Keywords           (fallback siempre disponible)

Claude da mejor comprensión contextual de noticias financieras y soporta
sarcasmo, dobles negaciones y jerga de mercado mejor que keywords simples.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass

from alpha_agent.config import GEMINI_MODEL, NEGATIVE_KEYWORDS, POSITIVE_KEYWORDS
from .news_fetcher import Headline

logger = logging.getLogger(__name__)


@dataclass
class SentimentSummary:
    score: float
    n_headlines: int
    positive_count: int
    negative_count: int
    neutral_count: int
    sample_titles: list[str]
    method: str = "keywords"    # "claude" | "gemini" | "keywords"


# ── Keyword fallback ──────────────────────────────────────────────────────────

def _score_one_keyword(text: str) -> int:
    if not text:
        return 0
    lowered = text.lower()
    pos = sum(1 for w in POSITIVE_KEYWORDS if w in lowered)
    neg = sum(1 for w in NEGATIVE_KEYWORDS if w in lowered)
    return 1 if pos > neg else (-1 if neg > pos else 0)


def score_headlines_keywords(headlines: list[Headline]) -> list[int]:
    return [_score_one_keyword(h.title) for h in headlines]


# ── Claude claude-haiku-4-5 (primary AI scorer) ─────────────────────────────────────

_CLAUDE_PROMPT = """\
You are a financial news sentiment classifier.
For each headline, return EXACTLY a JSON array of integers: 1 (bullish for the stock price), 0 (neutral), -1 (bearish).
One integer per headline, same order, nothing else outside the JSON array.

Headlines (JSON array of strings):
{headlines_json}"""

_CLAUDE_MODEL = "claude-haiku-4-5-20251001"


def _score_with_claude(headlines: list[Headline]) -> list[int] | None:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    titles = [h.title for h in headlines if h.title]
    if not titles:
        return [0] * len(headlines)
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=_CLAUDE_MODEL,
            max_tokens=len(titles) * 5 + 20,
            messages=[{
                "role": "user",
                "content": _CLAUDE_PROMPT.format(headlines_json=json.dumps(titles)),
            }],
        )
        text = msg.content[0].text.strip()
        match = re.search(r"\[.*?\]", text, re.DOTALL)
        if not match:
            return None
        parsed = json.loads(match.group())
        if not isinstance(parsed, list):
            return None
        scores = [max(-1, min(1, int(v))) for v in parsed]
        # Re-align with original list (may have empty titles)
        result, it = [], iter(scores)
        for h in headlines:
            result.append(next(it, 0) if h.title else 0)
        return result
    except Exception as exc:
        logger.debug("Claude sentiment failed (%s) — trying Gemini.", exc)
        return None


# ── Gemini Flash (secondary AI scorer) ───────────────────────────────────────

_GEMINI_PROMPT = """\
Financial news sentiment classifier. For each headline return ONLY a JSON array
of integers: 1 (positive for the stock), 0 (neutral), -1 (negative).
Exactly one integer per headline, nothing else.

Headlines:
{headlines_json}
"""


def _score_with_gemini(headlines: list[Headline]) -> list[int] | None:
    if not os.getenv("GOOGLE_API_KEY"):
        return None
    titles = [h.title for h in headlines if h.title]
    if not titles:
        return [0] * len(headlines)
    try:
        from google import genai
        client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=_GEMINI_PROMPT.format(headlines_json=json.dumps(titles)),
        )
        match = re.search(r"\[.*?\]", resp.text, re.DOTALL)
        if not match:
            return None
        parsed = json.loads(match.group())
        if not isinstance(parsed, list):
            return None
        scores = [max(-1, min(1, int(v))) for v in parsed]
        result, it = [], iter(scores)
        for h in headlines:
            result.append(next(it, 0) if h.title else 0)
        return result
    except Exception as exc:
        logger.debug("Gemini sentiment failed (%s) — fallback keywords.", exc)
        return None


# ── API pública ───────────────────────────────────────────────────────────────

def _best_scores(headlines: list[Headline]) -> tuple[list[int], str]:
    """Try Claude → Gemini → keywords. Returns (scores, method_name)."""
    result = _score_with_claude(headlines)
    if result is not None and len(result) == len(headlines):
        return result, "claude"

    result = _score_with_gemini(headlines)
    if result is not None and len(result) == len(headlines):
        return result, "gemini"

    return score_headlines_keywords(headlines), "keywords"


def score_headlines(headlines: list[Headline]) -> list[int]:
    scores, _ = _best_scores(headlines)
    return scores


def summarize_sentiment(headlines: list[Headline]) -> SentimentSummary:
    if not headlines:
        return SentimentSummary(0.0, 0, 0, 0, 0, [])

    scores, method = _best_scores(headlines)

    pos = sum(1 for s in scores if s > 0)
    neg = sum(1 for s in scores if s < 0)
    neu = sum(1 for s in scores if s == 0)
    avg = sum(scores) / len(scores)

    sorted_hl = sorted(zip(headlines, scores), key=lambda x: -abs(x[1]))
    sample = [h.title for h, _ in sorted_hl[:3]]

    logger.debug("Sentiment via %s: %.2f (%d+ %d- %dn)", method, avg, pos, neg, neu)

    return SentimentSummary(
        score=round(avg, 3),
        n_headlines=len(headlines),
        positive_count=pos,
        negative_count=neg,
        neutral_count=neu,
        sample_titles=sample,
        method=method,
    )
