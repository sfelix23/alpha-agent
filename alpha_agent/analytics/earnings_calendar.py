"""
Earnings calendar filter.

Evita entrar en posiciones CP/LP justo antes de que un ticker reporte resultados.
Un earnings sorpresa negativo puede liquidar una posición en minutos — este
filtro descarta tickers con earnings en los próximos N días del pipeline.

Sin API key adicional: usa yfinance.Ticker.calendar (datos públicos de Yahoo).
Silencia cualquier error — si no puede obtener el calendar, asume safe (no bloquea).
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

log = logging.getLogger(__name__)


def has_earnings_soon(ticker: str, days_ahead: int = 2) -> bool:
    """
    True si el ticker tiene earnings reportados en los próximos `days_ahead` días.
    False en cualquier caso de error o dato faltante (conservador: no bloquea).
    """
    try:
        import yfinance as yf

        cal = yf.Ticker(ticker).calendar
        if not cal:
            return False

        today  = date.today()
        cutoff = today + timedelta(days=days_ahead)

        # yfinance devuelve dict; la key puede variar por versión
        dates = (
            cal.get("Earnings Date")
            or cal.get("Earnings Dates")
            or cal.get("earningsDate")
            or []
        )
        if not isinstance(dates, (list, tuple)):
            dates = [dates]

        for ed in dates:
            if ed is None:
                continue
            try:
                # puede ser Timestamp, date, str
                if hasattr(ed, "date"):
                    ed_date = ed.date()
                elif hasattr(ed, "year"):
                    ed_date = date(ed.year, ed.month, ed.day)
                else:
                    ed_date = date.fromisoformat(str(ed)[:10])

                if today <= ed_date <= cutoff:
                    log.warning("⚠️  %s: earnings %s — filtrado del pipeline", ticker, ed_date)
                    return True
            except Exception:
                continue

    except Exception as exc:
        log.debug("earnings_calendar(%s): %s — asumiendo safe", ticker, exc)

    return False


def filter_earnings_risk(
    tickers: list[str],
    days_ahead: int = 2,
) -> tuple[list[str], list[str]]:
    """
    Separa la lista en (safe, risky).

    safe:  sin earnings en <= days_ahead días → pueden operar
    risky: con earnings próximos → omitir del pipeline de entrada

    El check es rápido (< 0.3s por ticker con cache de yfinance).
    """
    safe: list[str] = []
    risky: list[str] = []
    for t in tickers:
        (risky if has_earnings_soon(t, days_ahead) else safe).append(t)
    if risky:
        log.warning("Filtrados por earnings próximos (%dd): %s", days_ahead, risky)
    return safe, risky
