"""Sentiment scoring de headlines financieros.

Dos capas:
  1. LLM via claude_analyst.call_llm (cascada Groq → Gemini → fallback)
  2. Keywords (fallback determinista, siempre disponible)

El LLM gateway maneja toda la complejidad de provider selection, cache y
budget. Este archivo sólo arma el prompt batched y parsea la respuesta.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from alpha_agent.config import NEGATIVE_KEYWORDS, POSITIVE_KEYWORDS
from alpha_agent.news.claude_analyst import call_llm
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
    method: str = "keywords"    # "llm" | "keywords"


# ── Keyword fallback ─────────────────────────────────────────────────────────


def _score_one_keyword(text: str) -> int:
    if not text:
        return 0
    lowered = text.lower()
    pos = sum(1 for w in POSITIVE_KEYWORDS if w in lowered)
    neg = sum(1 for w in NEGATIVE_KEYWORDS if w in lowered)
    return 1 if pos > neg else (-1 if neg > pos else 0)


def score_headlines_keywords(headlines: list[Headline]) -> list[int]:
    return [_score_one_keyword(h.title) for h in headlines]


# ── LLM batch scoring ────────────────────────────────────────────────────────


_LLM_PROMPT = """\
Financial news sentiment classifier. For each headline return ONLY a JSON array
of integers: 1 (bullish for the stock), 0 (neutral), -1 (bearish).
Exactly one integer per headline, same order, nothing else outside the array.

Headlines (JSON array of strings):
{headlines_json}"""


def _score_with_llm(headlines: list[Headline]) -> list[int] | None:
    """Llama al gateway con purpose='sentiment'. None si todos los providers fallaron."""
    titles = [h.title for h in headlines if h.title]
    if not titles:
        return [0] * len(headlines)

    prompt = _LLM_PROMPT.format(headlines_json=json.dumps(titles))
    # cache_key_extra incluye el hash de los headlines así que prompts idénticos
    # comparten cache; pero si cambia un solo headline la key cambia.
    text = call_llm(prompt, purpose="sentiment", max_tokens=len(titles) * 5 + 20)
    if not text:
        return None

    match = re.search(r"\[.*?\]", text, re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group())
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, list):
        return None

    scores = [max(-1, min(1, int(v))) for v in parsed]
    # Re-align con la lista original (puede tener entradas con title vacío).
    result, it = [], iter(scores)
    for h in headlines:
        result.append(next(it, 0) if h.title else 0)
    return result


# ── API pública ──────────────────────────────────────────────────────────────


def _best_scores(headlines: list[Headline]) -> tuple[list[int], str]:
    """Intenta LLM primero, cae a keywords si LLM no responde."""
    llm_scores = _score_with_llm(headlines)
    if llm_scores is not None and len(llm_scores) == len(headlines):
        return llm_scores, "llm"
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
