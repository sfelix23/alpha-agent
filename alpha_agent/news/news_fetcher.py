"""
Fetching de noticias desde fuentes 100% gratis.

1. `fetch_ticker_news(ticker)` — usa yfinance.Ticker.news (sin API key).
2. `fetch_macro_news()` — parsea Google News RSS para queries macro.

Todo tiene cache en disco por día para no saturar ni a Yahoo ni a Google.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

from alpha_agent.config import MACRO_NEWS_QUERIES, PATHS

logger = logging.getLogger(__name__)


@dataclass
class Headline:
    title: str
    source: str
    published: str       # ISO string
    url: str
    related_query: str = ""   # para noticias macro
    ticker: str = ""          # para noticias de activo


def _cache_file(kind: str) -> Path:
    return PATHS.cache_dir / f"news_{kind}_{date.today().isoformat()}.json"


def _load_cache(path: Path) -> list[dict] | None:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _save_cache(path: Path, data: list[Headline]) -> None:
    path.write_text(
        json.dumps([asdict(h) for h in data], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ────────────────────────────────────────────────────────────────────────────
# 1) Noticias por activo vía yfinance
# ────────────────────────────────────────────────────────────────────────────
def fetch_ticker_news(ticker: str, *, max_items: int = 6) -> list[Headline]:
    cache_path = _cache_file(f"ticker_{ticker}")
    cached = _load_cache(cache_path)
    if cached is not None:
        return [Headline(**h) for h in cached]

    try:
        import yfinance as yf  # type: ignore
        raw = yf.Ticker(ticker).news or []
    except Exception as e:
        logger.debug("yfinance news falló para %s: %s", ticker, e)
        raw = []

    headlines: list[Headline] = []
    for item in raw[:max_items]:
        # yfinance cambió el schema de news varias veces; parseamos defensivo
        content = item.get("content", item) if isinstance(item, dict) else {}
        title = content.get("title") or item.get("title", "")
        provider = (
            content.get("provider", {}).get("displayName")
            if isinstance(content.get("provider"), dict)
            else item.get("publisher", "")
        ) or ""
        url = (
            content.get("canonicalUrl", {}).get("url")
            if isinstance(content.get("canonicalUrl"), dict)
            else item.get("link", "")
        ) or ""
        pub_raw = content.get("pubDate") or item.get("providerPublishTime", "")
        if isinstance(pub_raw, (int, float)):
            published = datetime.fromtimestamp(pub_raw).isoformat()
        else:
            published = str(pub_raw)

        if title:
            headlines.append(Headline(
                title=title,
                source=provider,
                published=published,
                url=url,
                ticker=ticker,
            ))

    _save_cache(cache_path, headlines)
    return headlines


# ────────────────────────────────────────────────────────────────────────────
# 2) Noticias macro vía Google News RSS
# ────────────────────────────────────────────────────────────────────────────
def _google_news_rss(query: str) -> str:
    q = urllib.parse.quote(query)
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


def fetch_macro_news(*, max_per_query: int = 5) -> list[Headline]:
    cache_path = _cache_file("macro")
    cached = _load_cache(cache_path)
    if cached is not None:
        return [Headline(**h) for h in cached]

    try:
        import feedparser  # type: ignore
    except ImportError:
        logger.warning("feedparser no instalado — macro news deshabilitado. `pip install feedparser`")
        return []

    out: list[Headline] = []
    for query in MACRO_NEWS_QUERIES:
        try:
            feed = feedparser.parse(_google_news_rss(query))
        except Exception as e:
            logger.debug("RSS fail para '%s': %s", query, e)
            continue
        for entry in feed.entries[:max_per_query]:
            out.append(Headline(
                title=getattr(entry, "title", ""),
                source=getattr(entry, "source", {}).get("title", "") if hasattr(entry, "source") else "",
                published=getattr(entry, "published", ""),
                url=getattr(entry, "link", ""),
                related_query=query,
            ))

    _save_cache(cache_path, out)
    logger.info("Macro news: %d headlines para %d queries", len(out), len(MACRO_NEWS_QUERIES))
    return out
