"""
Indicadores técnicos — versión mejorada.

Calcula por ticker:
  - RSI(14): sobreventa/sobrecompra
  - ATR(14): volatilidad real → input del stop loss
  - MACD(12,26,9): momentum y dirección de tendencia
  - Volume ratio: volumen actual vs promedio 20d (convicción del movimiento)
  - EMA 20 / EMA 50: tendencia y golden cross
  - Bollinger Band position: dónde está el precio dentro de las bandas
  - Momentum 1m / 3m
  - Distancia al máximo 52 semanas
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

try:
    import pandas_ta as ta
    _HAS_PANDAS_TA = True
except Exception:
    _HAS_PANDAS_TA = False


# ── Implementaciones nativas (no dependen de pandas_ta) ──────────────────────

def _rsi_native(close: pd.Series, length: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(length).mean()
    loss = -delta.clip(upper=0).rolling(length).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def _atr_native(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(length).mean()


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _macd_native(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """Retorna (macd_line, signal_line, histogram)."""
    ema_fast = _ema(close, fast)
    ema_slow = _ema(close, slow)
    macd_line = ema_fast - ema_slow
    signal_line = _ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def _bollinger_position(close: pd.Series, length: int = 20, std_dev: float = 2.0) -> pd.Series:
    """
    Posición del precio dentro de las Bollinger Bands.
    0 = en la banda inferior, 0.5 = en la media, 1 = en la banda superior.
    Valores > 1 o < 0 significan que el precio está fuera de las bandas.
    """
    sma = close.rolling(length).mean()
    std = close.rolling(length).std()
    upper = sma + std_dev * std
    lower = sma - std_dev * std
    band_width = upper - lower
    pos = (close - lower) / band_width.replace(0, np.nan)
    return pos


# ── API pública ───────────────────────────────────────────────────────────────

def compute_technical_indicators(ohlc: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """
    Calcula indicadores técnicos por ticker.

    Args:
        ohlc: dict {ticker: DataFrame con columnas Open/High/Low/Close/Volume}.

    Returns:
        DataFrame indexado por ticker con métricas técnicas enriquecidas.
    """
    rows: list[dict[str, Any]] = []

    for ticker, df in ohlc.items():
        if df is None or len(df) < 60:
            continue

        close  = df["Close"]
        high   = df["High"]
        low    = df["Low"]
        volume = df.get("Volume", pd.Series(dtype=float))

        # ── RSI y ATR ───────────────────────────────────────────────────────
        if _HAS_PANDAS_TA:
            try:
                rsi = ta.rsi(close, length=14)
                atr = ta.atr(high, low, close, length=14)
            except Exception:
                rsi = _rsi_native(close)
                atr = _atr_native(high, low, close)
        else:
            rsi = _rsi_native(close)
            atr = _atr_native(high, low, close)

        # ── MACD ────────────────────────────────────────────────────────────
        macd_line, macd_signal, macd_hist = _macd_native(close)
        last_macd      = float(macd_line.iloc[-1])  if not np.isnan(macd_line.iloc[-1])   else 0.0
        last_macd_sig  = float(macd_signal.iloc[-1]) if not np.isnan(macd_signal.iloc[-1]) else 0.0
        last_macd_hist = float(macd_hist.iloc[-1])  if not np.isnan(macd_hist.iloc[-1])   else 0.0
        # bullish = MACD > signal y histograma creciente
        macd_bullish = int(last_macd > last_macd_sig and last_macd_hist > 0)

        # ── EMA 20 / 50 ─────────────────────────────────────────────────────
        ema20 = _ema(close, 20)
        ema50 = _ema(close, 50)
        last_ema20    = float(ema20.iloc[-1])
        last_ema50    = float(ema50.iloc[-1])
        golden_cross  = int(last_ema20 > last_ema50)   # 1 = tendencia alcista
        above_ema50   = int(close.iloc[-1] > last_ema50)

        # ── SMA 200 (tendencia primaria) ─────────────────────────────────────
        sma200 = close.rolling(200).mean()
        last_sma200  = float(sma200.iloc[-1]) if len(close) >= 200 and not np.isnan(sma200.iloc[-1]) else float(close.mean())
        above_sma200 = int(close.iloc[-1] > last_sma200)

        # ── Bollinger Bands ──────────────────────────────────────────────────
        bb_pos = _bollinger_position(close)
        last_bb_pos = float(bb_pos.iloc[-1]) if not np.isnan(bb_pos.iloc[-1]) else 0.5
        # Compresión de bandas (squeeze) → posible explosión de volatilidad
        bb_width = close.rolling(20).std() / close.rolling(20).mean()
        bb_squeeze = int(
            float(bb_width.iloc[-1]) < float(bb_width.rolling(50).mean().iloc[-1]) * 0.75
        ) if len(bb_width.dropna()) >= 50 else 0

        # ── Volume ──────────────────────────────────────────────────────────
        vol_ratio = np.nan
        if len(volume.dropna()) >= 21:
            avg_vol_20 = float(volume.rolling(20).mean().iloc[-1])
            last_vol   = float(volume.iloc[-1])
            if avg_vol_20 > 0:
                vol_ratio = last_vol / avg_vol_20

        # ── Precios / Momentum ───────────────────────────────────────────────
        last_price = float(close.iloc[-1])
        last_rsi   = float(rsi.iloc[-1]) if rsi is not None and not np.isnan(rsi.iloc[-1]) else np.nan
        last_atr   = float(atr.iloc[-1]) if atr is not None and not np.isnan(atr.iloc[-1]) else np.nan

        ret_1m  = float(close.iloc[-1] / close.iloc[-21] - 1)  if len(close) >= 22 else np.nan
        ret_5d  = float(close.iloc[-1] / close.iloc[-5] - 1)   if len(close) >= 6 else np.nan
        ret_3m  = float(close.iloc[-1] / close.iloc[-63] - 1)  if len(close) >= 64 else np.nan
        ret_6m  = float(close.iloc[-1] / close.iloc[-126] - 1) if len(close) >= 127 else np.nan

        high_52w     = float(close.tail(252).max())
        dist_52w_high = float(last_price / high_52w - 1)

        # Chandelier Exit: trailing stop dinámico (Chuck LeBeau)
        # = highest_close(22) - 3 × ATR(22)
        atr22 = _atr_native(high, low, close, length=22)
        if len(close) >= 23 and not np.isnan(atr22.iloc[-1]):
            highest_close_22 = float(close.tail(22).max())
            chandelier_stop  = round(highest_close_22 - 3.0 * float(atr22.iloc[-1]), 2)
        else:
            chandelier_stop = np.nan

        # Breakout: precio en máximo de 20 días con volumen alto
        high_20d  = float(close.tail(20).max())
        breakout  = int(last_price >= high_20d * 0.995 and (vol_ratio or 0) > 1.5)

        rows.append({
            "ticker":        ticker,
            "price":         round(last_price, 2),
            # Clásicos
            "rsi":           round(last_rsi, 2) if not np.isnan(last_rsi) else np.nan,
            "atr":           round(last_atr, 4) if not np.isnan(last_atr) else np.nan,
            "stop_loss_atr": round(last_price - 2 * last_atr, 2) if not np.isnan(last_atr) else np.nan,
            "ret_5d":        ret_5d,
            "ret_1m":        ret_1m,
            "ret_3m":        ret_3m,
            "ret_6m":        ret_6m,
            "dist_52w_high": dist_52w_high,
            "chandelier_stop": chandelier_stop,
            # Nuevos — MACD
            "macd":          round(last_macd, 4),
            "macd_signal":   round(last_macd_sig, 4),
            "macd_hist":     round(last_macd_hist, 4),
            "macd_bullish":  macd_bullish,
            # Nuevos — EMA / SMA / tendencia
            "ema20":         round(last_ema20, 2),
            "ema50":         round(last_ema50, 2),
            "sma200":        round(last_sma200, 2),
            "golden_cross":  golden_cross,
            "above_ema50":   above_ema50,
            "above_sma200":  above_sma200,
            # Nuevos — Bollinger
            "bb_position":   round(last_bb_pos, 3),
            "bb_squeeze":    bb_squeeze,
            # Nuevos — Volumen
            "vol_ratio":     round(vol_ratio, 2) if not np.isnan(vol_ratio) else 1.0,
            # Nuevos — Breakout
            "breakout":      breakout,
        })

    return pd.DataFrame(rows).set_index("ticker")
