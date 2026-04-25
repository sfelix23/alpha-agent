"""
Reddit sentiment via PRAW — r/wallstreetbets + r/stocks.

Requiere en .env:
    REDDIT_CLIENT_ID
    REDDIT_CLIENT_SECRET
    REDDIT_USER_AGENT   (ej: "alpha_agent/1.0 by tu_usuario")

Si PRAW no está instalado o las credenciales faltan, falla silenciosamente
retornando scores neutros (0.0).

Score por ticker: media ponderada de (score del post × sentimiento keyword).
Rango de salida: [-1.0, +1.0].
"""

from __future__ import annotations

import logging
import os
import time
from functools import lru_cache

logger = logging.getLogger(__name__)

_CACHE: dict[str, tuple[float, dict[str, float]]] = {}
_CACHE_TTL = 3600  # 1h

_BULLISH = {"buy","bull","calls","moon","rocket","long","undervalued","breakout","accumulate","strong buy"}
_BEARISH = {"sell","bear","puts","short","dump","overvalued","crash","avoid","downgrade","weak"}

_SUBREDDITS = ["wallstreetbets", "stocks", "investing"]


def _keyword_score(text: str) -> float:
    words = set(text.lower().split())
    bull  = len(words & _BULLISH)
    bear  = len(words & _BEARISH)
    total = bull + bear
    if total == 0:
        return 0.0
    return (bull - bear) / total


@lru_cache(maxsize=1)
def _get_reddit():
    import praw
    return praw.Reddit(
        client_id=os.environ["REDDIT_CLIENT_ID"],
        client_secret=os.environ["REDDIT_CLIENT_SECRET"],
        user_agent=os.environ.get("REDDIT_USER_AGENT", "alpha_agent/1.0"),
    )


def get_reddit_sentiment(tickers: list[str], limit_per_sub: int = 50) -> dict[str, float]:
    """
    Devuelve {ticker: sentiment_score} en [-1, +1].
    Solo calcula si las credenciales REDDIT_* están en el entorno.
    """
    if not os.environ.get("REDDIT_CLIENT_ID"):
        return {t: 0.0 for t in tickers}

    now = time.monotonic()
    cache_key = ",".join(sorted(tickers))
    if cache_key in _CACHE:
        ts, result = _CACHE[cache_key]
        if now - ts < _CACHE_TTL:
            return result

    try:
        reddit  = _get_reddit()
        ticker_set = {t.upper() for t in tickers}
        mention_scores: dict[str, list[float]] = {t: [] for t in ticker_set}

        for sub_name in _SUBREDDITS:
            try:
                sub   = reddit.subreddit(sub_name)
                posts = list(sub.hot(limit=limit_per_sub))
                for post in posts:
                    text  = f"{post.title} {post.selftext}"
                    score = _keyword_score(text)
                    # Peso: upvote ratio × log(score+2)
                    import math
                    weight = (post.upvote_ratio or 0.5) * math.log(max(post.score, 0) + 2)
                    for ticker in ticker_set:
                        if ticker in text.upper():
                            mention_scores[ticker].append(score * weight)
            except Exception as exc:
                logger.debug("r/%s error: %s", sub_name, exc)

        result: dict[str, float] = {}
        for ticker, scores in mention_scores.items():
            if scores:
                avg = sum(scores) / len(scores)
                result[ticker] = round(max(-1.0, min(1.0, avg)), 3)
            else:
                result[ticker] = 0.0

        nonzero = {t: v for t, v in result.items() if v != 0.0}
        if nonzero:
            logger.info("Reddit sentiment: %s", {t: f"{v:+.3f}" for t, v in nonzero.items()})

        _CACHE[cache_key] = (now, result)
        return result

    except Exception as exc:
        logger.warning("Reddit sentiment falló: %s", exc)
        return {t: 0.0 for t in tickers}
