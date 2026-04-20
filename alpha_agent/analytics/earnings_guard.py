"""
Earnings Calendar Guard.

Fetches next earnings date for each ticker via yfinance.
Returns a set of tickers with earnings within `days` calendar days.
Used by scoring to penalise / warn about binary event risk.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

log = logging.getLogger(__name__)


def get_earnings_soon(tickers: list[str], days: int = 3) -> dict[str, date]:
    """
    Returns {ticker: earnings_date} for tickers with earnings within `days` calendar days.
    Fails silently per ticker; missing data is treated as no upcoming earnings.
    """
    import yfinance as yf

    cutoff = date.today() + timedelta(days=days)
    result: dict[str, date] = {}

    for ticker in tickers:
        try:
            cal = yf.Ticker(ticker).calendar
            if not cal:
                continue
            dates = cal.get("Earnings Date", [])
            if not dates:
                continue
            nearest = min(
                (d for d in dates if isinstance(d, date) and d >= date.today()),
                default=None,
            )
            if nearest and nearest <= cutoff:
                result[ticker] = nearest
        except Exception:
            pass

    if result:
        log.info("Earnings en los proximos %d dias: %s", days, result)
    return result
