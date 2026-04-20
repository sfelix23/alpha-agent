"""
Capa de noticias: fetching + sentiment + contexto fundamental.

Fuentes:
    - yfinance.Ticker.news → headlines por activo (gratis, sin key).
    - Google News RSS      → queries macro (Trump, petróleo, etc.).

Sentiment: scoring keyword-based (ver config.POSITIVE/NEGATIVE_KEYWORDS).
"""
from .news_fetcher import fetch_ticker_news, fetch_macro_news
from .sentiment import score_headlines, summarize_sentiment

__all__ = [
    "fetch_ticker_news",
    "fetch_macro_news",
    "score_headlines",
    "summarize_sentiment",
]
