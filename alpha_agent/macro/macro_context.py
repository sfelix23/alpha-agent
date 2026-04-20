"""
Snapshot macro del mundo.

Descarga precios spot y % change de:
    - Petróleo WTI y Brent
    - Oro
    - Índice dólar (DXY)
    - VIX
    - 10y US Treasury yield

Y detecta el régimen de mercado actual del S&P 500:
    - bull: SPY > SMA200 y pendiente positiva
    - bear: SPY < SMA200 y pendiente negativa
    - sideways: caso intermedio

El régimen se usa para ajustar el peso del sleeve de corto plazo (en mercado
lateral CP pierde efectividad) y para redactar la tesis financiera.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

import pandas as pd
import yfinance as yf

from alpha_agent.config import MACRO_TICKERS, PARAMS, PATHS

logger = logging.getLogger(__name__)


@dataclass
class MacroSnapshot:
    as_of: str
    prices: dict[str, float] = field(default_factory=dict)        # nombre → precio
    changes_1d: dict[str, float] = field(default_factory=dict)    # nombre → %
    changes_1m: dict[str, float] = field(default_factory=dict)
    regime: str = "unknown"          # bull | bear | sideways | unknown
    regime_reason: str = ""
    spy_vs_sma200: float = 0.0       # % distancia


def _cache_path() -> str:
    return str(PATHS.cache_dir / f"macro_{date.today().isoformat()}.pkl")


def fetch_macro_snapshot() -> MacroSnapshot:
    snapshot = MacroSnapshot(as_of=date.today().isoformat())

    # 1) Macro tickers
    tickers = list(MACRO_TICKERS.values())
    try:
        raw = yf.download(
            tickers=tickers,
            period="6mo",
            interval="1d",
            auto_adjust=True,
            progress=False,
            group_by="column",
            threads=True,
        )
    except Exception as e:
        logger.warning("No se pudo descargar macro: %s", e)
        return snapshot

    if raw is None or raw.empty:
        return snapshot

    if isinstance(raw.columns, pd.MultiIndex):
        close = raw["Close"]
    else:
        close = raw[["Close"]].rename(columns={"Close": tickers[0]})

    for name, ticker in MACRO_TICKERS.items():
        if ticker not in close.columns:
            continue
        series = close[ticker].dropna()
        if len(series) < 22:
            continue
        last = float(series.iloc[-1])
        prev = float(series.iloc[-2])
        month_ago = float(series.iloc[-22])
        snapshot.prices[name] = round(last, 3)
        snapshot.changes_1d[name] = round(last / prev - 1, 4)
        snapshot.changes_1m[name] = round(last / month_ago - 1, 4)

    # 2) Régimen de mercado basado en SPY
    try:
        spy_raw = yf.download("SPY", period="2y", interval="1d", auto_adjust=True, progress=False)
        if spy_raw is not None and not spy_raw.empty:
            if isinstance(spy_raw.columns, pd.MultiIndex):
                spy_raw.columns = spy_raw.columns.get_level_values(0)
            spy = spy_raw["Close"].dropna()
            sma200 = spy.rolling(200).mean()
            last_spy = float(spy.iloc[-1])
            last_sma = float(sma200.iloc[-1]) if not sma200.empty else float("nan")
            slope_30d = float(sma200.iloc[-1] / sma200.iloc[-31] - 1) if len(sma200.dropna()) >= 31 else 0.0

            dist = (last_spy / last_sma - 1) if last_sma and last_sma > 0 else 0.0
            snapshot.spy_vs_sma200 = round(dist, 4)

            if dist > 0.02 and slope_30d > 0:
                snapshot.regime = "bull"
                snapshot.regime_reason = f"SPY {dist*100:+.1f}% sobre SMA200 con pendiente positiva"
            elif dist < -0.02 and slope_30d < 0:
                snapshot.regime = "bear"
                snapshot.regime_reason = f"SPY {dist*100:+.1f}% bajo SMA200 con pendiente negativa"
            else:
                snapshot.regime = "sideways"
                snapshot.regime_reason = f"SPY {dist*100:+.1f}% de la SMA200, pendiente {slope_30d*100:+.1f}%"
    except Exception as e:
        logger.debug("Régimen fallback: %s", e)

    return snapshot


def detect_market_regime(snapshot: MacroSnapshot) -> str:
    """Helper separado por si querés llamar detección con un snapshot cargado."""
    return snapshot.regime
