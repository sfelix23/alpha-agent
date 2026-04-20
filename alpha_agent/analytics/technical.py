"""
Indicadores técnicos para el sleeve de corto plazo:

- RSI(14): identifica sobreventa/sobrecompra.
- ATR(14): mide volatilidad real → input del stop loss.
- Momentum 1m / 3m: retorno simple sobre 21 / 63 días.
- 52w high distance: cuán cerca está del máximo anual.

Si pandas_ta está instalado lo usamos; si no, hay implementación nativa
para no romper la cadena.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

try:
    import pandas_ta as ta  # type: ignore
    _HAS_PANDAS_TA = True
except Exception:
    _HAS_PANDAS_TA = False


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


def compute_technical_indicators(ohlc: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """
    Calcula indicadores técnicos por ticker.

    Args:
        ohlc: dict {ticker: DataFrame con columnas Open/High/Low/Close/Volume}.

    Returns:
        DataFrame indexado por ticker con métricas técnicas.
    """
    rows: list[dict[str, Any]] = []
    for ticker, df in ohlc.items():
        if df is None or len(df) < 60:
            continue

        close = df["Close"]
        high = df["High"]
        low = df["Low"]

        if _HAS_PANDAS_TA:
            try:
                rsi = ta.rsi(close, length=14)
                atr = ta.atr(high, low, close, length=14)
            except Exception:  # algunos tickers raros rompen pandas_ta
                rsi = _rsi_native(close)
                atr = _atr_native(high, low, close)
        else:
            rsi = _rsi_native(close)
            atr = _atr_native(high, low, close)

        last_price = float(close.iloc[-1])
        last_rsi = float(rsi.iloc[-1]) if rsi is not None and not np.isnan(rsi.iloc[-1]) else np.nan
        last_atr = float(atr.iloc[-1]) if atr is not None and not np.isnan(atr.iloc[-1]) else np.nan

        # momentum
        ret_1m = float(close.iloc[-1] / close.iloc[-21] - 1) if len(close) >= 22 else np.nan
        ret_3m = float(close.iloc[-1] / close.iloc[-63] - 1) if len(close) >= 64 else np.nan

        # distancia al máximo de 52 semanas (en %)
        high_52w = float(close.tail(252).max())
        dist_52w_high = float(last_price / high_52w - 1)

        rows.append({
            "ticker": ticker,
            "price": round(last_price, 2),
            "rsi": round(last_rsi, 2),
            "atr": round(last_atr, 2),
            "stop_loss_atr": round(last_price - 2 * last_atr, 2) if not np.isnan(last_atr) else np.nan,
            "ret_1m": ret_1m,
            "ret_3m": ret_3m,
            "dist_52w_high": dist_52w_high,
        })

    return pd.DataFrame(rows).set_index("ticker")
