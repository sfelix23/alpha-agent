"""
Macro event calendar 2026 — FOMC, CPI, NFP.

Devuelve los eventos que caen mañana o hoy para el macro guard en el trader.
"""

from __future__ import annotations

from datetime import date, timedelta

# FOMC meetings 2026 (decision dates)
_FOMC_2026 = [
    date(2026, 1, 29),
    date(2026, 3, 19),
    date(2026, 5, 7),
    date(2026, 6, 18),
    date(2026, 7, 30),
    date(2026, 9, 17),
    date(2026, 11, 5),
    date(2026, 12, 17),
]

# CPI release dates 2026 (BLS, 8:30 ET)
_CPI_2026 = [
    date(2026, 1, 14),
    date(2026, 2, 11),
    date(2026, 3, 11),
    date(2026, 4, 10),
    date(2026, 5, 13),
    date(2026, 6, 10),
    date(2026, 7, 14),
    date(2026, 8, 12),
    date(2026, 9, 10),
    date(2026, 10, 13),
    date(2026, 11, 12),
    date(2026, 12, 10),
]

# NFP (Non-Farm Payrolls) — first Friday of each month
_NFP_2026 = [
    date(2026, 1, 9),
    date(2026, 2, 6),
    date(2026, 3, 6),
    date(2026, 4, 3),
    date(2026, 5, 1),
    date(2026, 6, 5),
    date(2026, 7, 2),
    date(2026, 8, 7),
    date(2026, 9, 4),
    date(2026, 10, 2),
    date(2026, 11, 6),
    date(2026, 12, 4),
]

_CALENDAR: list[tuple[date, str]] = (
    [(d, "FOMC") for d in _FOMC_2026]
    + [(d, "CPI")  for d in _CPI_2026]
    + [(d, "NFP")  for d in _NFP_2026]
)


def get_upcoming_events(days_ahead: int = 1) -> list[str]:
    """
    Devuelve nombres de eventos macro dentro de los próximos `days_ahead` días.
    Si hay un evento → el trader reduce posiciones CP al 50%.
    """
    today = date.today()
    horizon = today + timedelta(days=days_ahead)
    return [
        name
        for event_date, name in _CALENDAR
        if today <= event_date <= horizon
    ]
