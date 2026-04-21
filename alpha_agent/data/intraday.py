"""
Señales intraday de 15 minutos para el sleeve de Corto Plazo.

Calcula bonus de scoring basado en:
  - Momentum intradía: precio actual vs apertura de hoy
  - Desviación del VWAP: precio vs precio medio ponderado por volumen
  - Aceleración de volumen: volumen de las últimas 2 velas vs promedio previo
  - Dirección de las últimas 3 velas (bullish streak)

Falla silenciosamente por ticker si yfinance no devuelve datos.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

_INTRADAY_CACHE: dict[str, tuple[pd.DataFrame, datetime]] = {}
_CACHE_TTL_MIN = 15


def _fetch_15m(ticker: str) -> pd.DataFrame | None:
    """Descarga datos de 15 minutos del último día con cache de 15 min."""
    now = datetime.now(tz=timezone.utc)
    if ticker in _INTRADAY_CACHE:
        df, ts = _INTRADAY_CACHE[ticker]
        if (now - ts).total_seconds() < _CACHE_TTL_MIN * 60:
            return df

    try:
        import yfinance as yf
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            df = yf.download(
                ticker,
                period="2d",
                interval="15m",
                progress=False,
                auto_adjust=True,
            )
        if df is None or df.empty or len(df) < 8:
            return None
        # aplanar columnas multi-nivel si existen
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        _INTRADAY_CACHE[ticker] = (df, now)
        return df
    except Exception as e:
        log.debug("intraday fetch %s: %s", ticker, e)
        return None


def _vwap(df: pd.DataFrame) -> float:
    """VWAP del día usando los datos de 15 min disponibles."""
    try:
        typical = (df["High"] + df["Low"] + df["Close"]) / 3
        vol = df["Volume"].replace(0, np.nan)
        return float((typical * vol).sum() / vol.sum())
    except Exception:
        return float(df["Close"].iloc[-1])


def _intraday_score(df: pd.DataFrame) -> float:
    """
    Score en [-1, +1] combinando 4 señales intraday:
      1. Momentum intradía (apertura → cierre actual)
      2. Desviación del VWAP (% por encima o debajo)
      3. Aceleración de volumen (últimas 2 velas vs media)
      4. Streak bullish de las últimas 3 velas
    """
    if df is None or len(df) < 4:
        return 0.0

    close = df["Close"]
    volume = df["Volume"]
    current = float(close.iloc[-1])
    open_today = float(close.iloc[0])

    # 1. Momentum intradía
    intra_mom = (current - open_today) / open_today if open_today else 0.0
    mom_signal = np.clip(intra_mom / 0.02, -1, 1)  # normalizado a ±2%

    # 2. Desviación VWAP
    vwap = _vwap(df)
    vwap_dev = (current - vwap) / vwap if vwap else 0.0
    vwap_signal = np.clip(vwap_dev / 0.015, -1, 1)

    # 3. Aceleración de volumen (últimas 2 vs media de las anteriores)
    if len(volume) >= 6:
        recent_vol = float(volume.iloc[-2:].mean())
        base_vol   = float(volume.iloc[-6:-2].mean())
        vol_ratio  = recent_vol / base_vol if base_vol > 0 else 1.0
        vol_signal = np.clip((vol_ratio - 1.0) / 1.0, -1, 1)
    else:
        vol_signal = 0.0

    # 4. Bullish streak (últimas 3 velas)
    returns_3 = close.iloc[-3:].pct_change().dropna()
    streak_score = float((returns_3 > 0).sum() - (returns_3 < 0).sum()) / 3.0

    score = (
        0.35 * mom_signal
        + 0.25 * vwap_signal
        + 0.25 * vol_signal
        + 0.15 * streak_score
    )
    return float(np.clip(score, -1.0, 1.0))


def fetch_intraday_signals(tickers: list[str]) -> dict[str, float]:
    """
    Devuelve {ticker: intraday_score} para cada ticker.
    Score en [-1, +1]. Falla silenciosamente por ticker.
    """
    out: dict[str, float] = {}
    for ticker in tickers:
        df = _fetch_15m(ticker)
        if df is not None:
            out[ticker] = _intraday_score(df)
        else:
            out[ticker] = 0.0

    valid = {t: s for t, s in out.items() if s != 0.0}
    if valid:
        top = sorted(valid.items(), key=lambda x: x[1], reverse=True)[:3]
        log.info("Intraday top CP: %s", [(t, f"{s:+.2f}") for t, s in top])

    return out
