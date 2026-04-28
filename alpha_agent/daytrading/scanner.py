"""
Day Trading scanner — detecta setups intraday de alta probabilidad.

Universo: acciones US de alta liquidez (QQQ mega-caps + momentum names).
Lógica: gap alcista + precio > VWAP + volumen acelerado + RSI no sobrecomprado.

Filtros de entrada:
  gap     > +1.5%  desde el cierre anterior
  price   > VWAP   (tendencia intraday confirmada)
  vol_ratio > 1.5x (dinero institucional entrando)
  RSI(15m) 42–74   (ni dormido ni sobrecomprado)
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# ── Universo DT: muy líquidos, spreads ajustados, suficiente volatilidad ──────
DT_UNIVERSE: list[str] = [
    # Mega-caps (liquidez máxima, gaps frecuentes con noticias/earnings)
    "NVDA", "AMD", "TSLA", "META", "GOOGL", "AMZN", "AAPL", "MSFT", "AVGO", "NFLX",
    # High-beta momentum (más volátiles, mejores para DT)
    "COIN", "CRWD", "PLTR", "MELI", "SOFI",
    # Energía / materiales (gaps con crude oil / macro)
    "XOM", "CVX", "SLB", "FCX",
    # Defensa / cripto ETF (momentum fuerte con noticias geopolíticas)
    "LMT", "RTX", "IBIT",
]

MIN_GAP_PCT   = 0.015   # gap mínimo desde cierre anterior (+1.5%)
MIN_VOL_RATIO = 1.5     # volumen reciente vs media histórica
RSI_MIN       = 42.0    # evita stocks dormidos o en colapso
RSI_MAX       = 74.0    # evita compra en sobrecompra extrema
MIN_DT_SCORE  = 0.20    # umbral mínimo de score combinado


def _fetch_15m(ticker: str) -> pd.DataFrame | None:
    try:
        import warnings
        import yfinance as yf
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            df = yf.download(ticker, period="2d", interval="15m",
                             progress=False, auto_adjust=True)
        if df is None or df.empty or len(df) < 8:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df
    except Exception as e:
        log.debug("fetch_15m %s: %s", ticker, e)
        return None


def _vwap(df: pd.DataFrame) -> float:
    typical = (df["High"] + df["Low"] + df["Close"]) / 3
    vol = df["Volume"].replace(0, np.nan)
    return float((typical * vol).sum() / vol.sum())


def _rsi(series: pd.Series, period: int = 14) -> float:
    delta = series.diff().dropna()
    gains = delta.clip(lower=0)
    losses = (-delta).clip(lower=0)
    avg_gain = gains.ewm(com=period - 1, min_periods=period).mean().iloc[-1]
    avg_loss = losses.ewm(com=period - 1, min_periods=period).mean().iloc[-1]
    if avg_loss == 0:
        return 100.0
    return float(100 - (100 / (1 + avg_gain / avg_loss)))


def _score_ticker(df: pd.DataFrame) -> tuple[float, dict]:
    """
    Score [0, 1] para un candidato DT. Retorna (score, metrics).

    Señales:
      gap_score    0.35 — gap vs cierre anterior
      vwap_score   0.30 — precio sobre VWAP
      vol_score    0.25 — aceleración de volumen
      rsi_score    0.10 — RSI en zona neutra-alcista
    """
    metrics: dict = {}

    today_str = df.index[-1].strftime("%Y-%m-%d")
    today_df = df[df.index.strftime("%Y-%m-%d") == today_str]
    prev_df  = df[df.index.strftime("%Y-%m-%d") < today_str]

    if len(today_df) < 4 or len(prev_df) < 4:
        return 0.0, {}

    prev_close  = float(prev_df["Close"].iloc[-1])
    today_open  = float(today_df["Close"].iloc[0])
    current     = float(today_df["Close"].iloc[-1])

    # 1. Gap
    gap_pct = (today_open - prev_close) / prev_close if prev_close > 0 else 0.0
    metrics["gap_pct"] = round(gap_pct, 4)
    if gap_pct < MIN_GAP_PCT:
        return 0.0, metrics
    gap_score = float(np.clip(gap_pct / 0.06, 0.0, 1.0))  # saturado en +6%

    # 2. VWAP
    vwap_val = _vwap(today_df)
    metrics["vwap"] = round(vwap_val, 2)
    vwap_dev = (current - vwap_val) / vwap_val if vwap_val > 0 else 0.0
    metrics["vwap_dev_pct"] = round(vwap_dev, 4)
    if vwap_dev < 0.0:
        return 0.0, metrics  # precio bajo el VWAP → tendencia bajista intraday
    vwap_score = float(np.clip(vwap_dev / 0.02, 0.0, 1.0))

    # 3. Volumen
    vol = today_df["Volume"]
    if len(vol) >= 6:
        recent    = float(vol.iloc[-2:].mean())
        base      = float(vol.iloc[:-2].mean())
        vol_ratio = recent / base if base > 0 else 1.0
    else:
        vol_ratio = 1.0
    metrics["vol_ratio"] = round(vol_ratio, 2)
    if vol_ratio < MIN_VOL_RATIO:
        return 0.0, metrics
    vol_score = float(np.clip((vol_ratio - 1.0) / 3.0, 0.0, 1.0))  # saturado en 4x

    # 4. RSI
    rsi_val = _rsi(today_df["Close"])
    metrics["rsi"] = round(rsi_val, 1)
    if not (RSI_MIN <= rsi_val <= RSI_MAX):
        return 0.0, metrics
    # RSI óptimo 55–65: score 1.0; alejar de ese centro reduce el score
    rsi_score = float(np.clip(1.0 - abs(rsi_val - 60.0) / 15.0, 0.0, 1.0))

    score = (
        0.35 * gap_score
        + 0.30 * vwap_score
        + 0.25 * vol_score
        + 0.10 * rsi_score
    )

    metrics.update({
        "current_price": round(current, 2),
        "prev_close":    round(prev_close, 2),
        "gap_score":     round(gap_score, 3),
        "vwap_score":    round(vwap_score, 3),
        "vol_score":     round(vol_score, 3),
        "rsi_score":     round(rsi_score, 3),
        "dt_score":      round(score, 3),
    })
    return float(score), metrics


def scan_dt_candidates(
    exclude_tickers: set[str] | None = None,
    limit: int = 2,
) -> list[dict]:
    """
    Escanea DT_UNIVERSE y retorna los mejores candidatos intraday.

    Returns list of dicts:
      ticker, dt_score, current_price, gap_pct, vwap, vwap_dev_pct,
      vol_ratio, rsi, stop_loss, take_profit
    """
    exclude    = exclude_tickers or set()
    candidates = []

    for ticker in DT_UNIVERSE:
        if ticker in exclude:
            continue
        df = _fetch_15m(ticker)
        if df is None:
            continue

        score, metrics = _score_ticker(df)
        if score < MIN_DT_SCORE:
            log.debug("DT skip %s: score=%.3f", ticker, score)
            continue

        price = metrics.get("current_price", 0.0)
        if price <= 0:
            continue

        # Bracket fijo desde precio actual de mercado
        sl = round(price * 0.985, 2)   # -1.5%
        tp = round(price * 1.035, 2)   # +3.5%  (R/R 2.33:1)

        candidates.append({
            "ticker":       ticker,
            "dt_score":     round(score, 3),
            "current_price": price,
            "gap_pct":      metrics.get("gap_pct", 0.0),
            "vwap":         metrics.get("vwap", 0.0),
            "vwap_dev_pct": metrics.get("vwap_dev_pct", 0.0),
            "vol_ratio":    metrics.get("vol_ratio", 0.0),
            "rsi":          metrics.get("rsi", 0.0),
            "stop_loss":    sl,
            "take_profit":  tp,
        })
        log.info(
            "DT candidato %s: score=%.3f gap=%.1f%% vol=%.1fx RSI=%.0f",
            ticker, score,
            metrics.get("gap_pct", 0.0) * 100,
            metrics.get("vol_ratio", 0.0),
            metrics.get("rsi", 0.0),
        )

    candidates.sort(key=lambda x: x["dt_score"], reverse=True)
    top = candidates[:limit]
    if top:
        log.info("DT top picks: %s", [(c["ticker"], c["dt_score"]) for c in top])
    else:
        log.info("DT: sin candidatos sobre umbral %.2f", MIN_DT_SCORE)
    return top
