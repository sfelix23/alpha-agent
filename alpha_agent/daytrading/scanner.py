"""
Day Trading scanner — detecta el mejor setup intraday del dia.

Estrategia concentrada: 1 sola posicion, maximo capital desplegado.
Con $1600 en la cuenta DT, se busca el ticker con mejor setup y se
entra concentrado — no tiene sentido diversificar en DT con capital chico.

Filtros de entrada:
  price   < MAX_PRICE  (minimo 5 shares a $1400 budget → maximo ~$280/accion)
  gap     > +1.5%      desde el cierre anterior
  price   > VWAP       (tendencia intraday confirmada)
  vol_ratio > 1.5x     (dinero institucional entrando)
  RSI(15m) 42–74       (ni dormido ni sobrecomprado)
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# ── Universo DT: líquidos, spreads ajustados, precio accesible con $1400 ──────
# Se excluyen stocks > ~$280 porque con $1400 quedamos con < 5 shares
# y el P&L en dólares es irrelevante (1 share × 3.5% = $31 máximo).
DT_UNIVERSE: list[str] = [
    # Bajo $150 — muchas shares, P&L proporcional al capital
    "AMD",   # ~$120  → ~11 shares
    "GOOGL", # ~$165  → ~8 shares
    "AVGO",  # ~$175  → ~8 shares
    "AMZN",  # ~$210  → ~6 shares
    "AAPL",  # ~$210  → ~6 shares
    "COIN",  # ~$220  → ~6 shares
    "TSLA",  # ~$280  → ~5 shares (límite)
    # Alta volatilidad — gaps frecuentes y bruscos
    "PLTR",  # ~$25   → ~56 shares
    "SOFI",  # ~$12   → ~116 shares
    "FCX",   # ~$45   → ~31 shares
    "SLB",   # ~$40   → ~35 shares
    # Energía — reacciona a macro (crude, OPEC, DXY)
    "XOM",   # ~$115  → ~12 shares
    "CVX",   # ~$155  → ~9 shares
    # Defensa — gaps con noticias geopolíticas
    "RTX",   # ~$130  → ~10 shares
    "LMT",   # ~$470  → 3 shares → solo entra si supera el filtro MIN_QTY
]

# Precio máximo por accion: con $1400 budget, queremos minimo 5 shares
# para que el P&L en dólares sea significativo
MAX_PRICE_USD = 280.0   # $1400 / 5 shares = $280 máximo
MIN_QTY_SHARES = 5      # minimo de shares para que valga operar

MIN_GAP_PCT   = 0.015   # gap mínimo desde cierre anterior (+1.5%)
MIN_VOL_RATIO = 1.5     # volumen reciente vs media histórica
RSI_MIN       = 42.0    # evita stocks dormidos o en colapso
RSI_MAX       = 74.0    # evita compra en sobrecompra extrema
MIN_DT_SCORE  = 0.20    # umbral mínimo de score combinado

# Bracket: TP ampliado para capturar tendencias intraday más largas
# SL -1.5% (ajustado), TP1 +2.5% (primer objetivo, 50% del capital),
# TP2 +5.0% (segundo objetivo, 50% restante). Estrategia dual-bracket.
DT_SL_PCT  = 0.015   # -1.5% stop loss
DT_TP1_PCT = 0.025   # +2.5% primer take profit (50% del tamaño)
DT_TP2_PCT = 0.050   # +5.0% segundo take profit (50% restante)


def _spy_is_bullish() -> bool:
    """
    Filtro de mercado: SPY cotiza por encima de su VWAP intraday.
    Si el mercado general está bajando, no hacemos DT largo.
    Falla silenciosamente (retorna True) si no hay datos.
    """
    df = _fetch_15m_raw("SPY")
    if df is None or len(df) < 4:
        return True
    today_str = df.index[-1].strftime("%Y-%m-%d")
    today_df  = df[df.index.strftime("%Y-%m-%d") == today_str]
    if len(today_df) < 2:
        return True
    current = float(today_df["Close"].iloc[-1])
    vwap    = _vwap(today_df)
    bullish = current > vwap
    log.debug("SPY VWAP filter: %.2f vs VWAP %.2f -> %s", current, vwap, "BULL" if bullish else "BEAR")
    return bullish


def _orb_score(today_df: pd.DataFrame) -> float:
    """
    Opening Range Breakout score [0, 1].
    ORB high = máximo de las primeras 2 velas (primeros 30 min de mercado).
    Si el precio actual supera el ORB high con momentum, el score es 1.0.
    Si está en el ORB o por debajo, el score es 0.
    """
    if len(today_df) < 3:
        return 0.0
    orb_high = float(today_df["High"].iloc[:2].max())
    current  = float(today_df["Close"].iloc[-1])
    if current <= orb_high:
        return 0.0
    # Score proporcional al breakout sobre el ORB
    breakout_pct = (current - orb_high) / orb_high
    return float(np.clip(breakout_pct / 0.015, 0.0, 1.0))  # saturado en +1.5% sobre ORB


def _fetch_15m_raw(ticker: str) -> pd.DataFrame | None:
    """Descarga 2 días de datos de 15 min (sin logs de debug en este nivel)."""
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

    # 5. ORB (Opening Range Breakout): precio sobre el máximo de los primeros 30min
    orb = _orb_score(today_df)
    metrics["orb_score"] = round(orb, 3)

    # Pesos: gap lidera, ORB confirma tendencia, VWAP y volumen validan
    score = (
        0.30 * gap_score
        + 0.25 * orb
        + 0.20 * vwap_score
        + 0.20 * vol_score
        + 0.05 * rsi_score
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
    budget_per_pos: float = 1400.0,
    limit: int = 1,
) -> list[dict]:
    """
    Escanea DT_UNIVERSE y retorna el mejor candidato del dia.

    Modo concentrado: limit=1 por defecto — 1 sola posicion con todo el budget.
    Filtra stocks donde budget_per_pos / price < MIN_QTY_SHARES.

    Returns list of dicts:
      ticker, dt_score, current_price, qty_shares, notional,
      gap_pct, vwap, vwap_dev_pct, vol_ratio, rsi,
      stop_loss, take_profit
    """
    exclude    = exclude_tickers or set()
    candidates = []

    # Filtro de mercado global: solo operar long cuando SPY está sobre su VWAP
    if not _spy_is_bullish():
        log.info("DT: SPY por debajo de VWAP — mercado bajista intraday. Sin entradas.")
        return []

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
        if price <= 0 or price > MAX_PRICE_USD:
            log.debug("DT skip %s: precio %.2f > maximo %.0f", ticker, price, MAX_PRICE_USD)
            continue

        qty = int(budget_per_pos / price)
        if qty < MIN_QTY_SHARES:
            log.debug("DT skip %s: solo %d shares con $%.0f", ticker, qty, budget_per_pos)
            continue

        # Dual bracket: mitad cierra en TP1, mitad en TP2
        qty1     = qty // 2         # primer tramo (toma ganancia rápida)
        qty2     = qty - qty1       # segundo tramo (deja correr la tendencia)
        notional = qty * price
        sl  = round(price * (1 - DT_SL_PCT),  2)
        tp1 = round(price * (1 + DT_TP1_PCT), 2)
        tp2 = round(price * (1 + DT_TP2_PCT), 2)

        candidates.append({
            "ticker":        ticker,
            "dt_score":      round(score, 3),
            "current_price": price,
            "qty_shares":    qty,
            "qty1":          qty1,    # shares para TP1 (+2.5%)
            "qty2":          qty2,    # shares para TP2 (+5.0%)
            "notional":      round(notional, 2),
            "gap_pct":       metrics.get("gap_pct", 0.0),
            "orb_score":     metrics.get("orb_score", 0.0),
            "vwap":          metrics.get("vwap", 0.0),
            "vwap_dev_pct":  metrics.get("vwap_dev_pct", 0.0),
            "vol_ratio":     metrics.get("vol_ratio", 0.0),
            "rsi":           metrics.get("rsi", 0.0),
            "stop_loss":     sl,
            "take_profit_1": tp1,
            "take_profit_2": tp2,
        })
        log.info(
            "DT candidato %s: score=%.3f precio=$%.2f shares=%d gap=%.1f%% vol=%.1fx RSI=%.0f",
            ticker, score, price, qty,
            metrics.get("gap_pct", 0.0) * 100,
            metrics.get("vol_ratio", 0.0),
            metrics.get("rsi", 0.0),
        )

    candidates.sort(key=lambda x: x["dt_score"], reverse=True)
    top = candidates[:limit]
    if top:
        best = top[0]
        log.info(
            "DT BEST: %s score=%.3f | %d shares x $%.2f = $%.0f | SL $%.2f TP1 $%.2f TP2 $%.2f",
            best["ticker"], best["dt_score"], best["qty_shares"],
            best["current_price"], best["notional"],
            best["stop_loss"], best["take_profit_1"], best["take_profit_2"],
        )
    else:
        log.info("DT: sin candidatos sobre umbral %.2f", MIN_DT_SCORE)
    return top
