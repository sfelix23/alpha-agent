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

MIN_GAP_PCT   = 0.005   # gap mínimo desde cierre anterior (+0.5%, era 1.0%)
MIN_VOL_RATIO = 1.1     # volumen reciente vs media histórica (era 1.2x)
RSI_MIN       = 35.0    # era 38 — acepta momentum naciente
RSI_MAX       = 80.0    # era 78 — permite entrar en momentum fuerte
MIN_DT_SCORE  = 0.12    # umbral mínimo de score combinado (era 0.15)

# Bracket: TP2 extendido a 7% para capturar tendencias intraday largas.
# La estrategia dual captura siempre algo (TP1) y deja correr la mitad (TP2).
# SL ajustado: -1.5% (riesgo contenido, R/R favorable).
DT_SL_PCT  = 0.015   # -1.5% stop loss (sin cambio)
DT_TP1_PCT = 0.030   # +3.0% primer take profit — era 2.5%, más margen
DT_TP2_PCT = 0.070   # +7.0% segundo take profit — era 5.0%, deja correr tendencias


def _spy_direction() -> str:
    """
    Determina la dirección intraday del mercado:
      "BULL" → SPY sobre VWAP → operar LONG
      "BEAR" → SPY bajo VWAP  → operar SHORT
      "FLAT" → sin datos suficientes → omitir
    """
    df = _fetch_15m_raw("SPY")
    if df is None or len(df) < 4:
        return "FLAT"
    today_str = df.index[-1].strftime("%Y-%m-%d")
    today_df  = df[df.index.strftime("%Y-%m-%d") == today_str]
    if len(today_df) < 2:
        return "FLAT"
    current   = float(today_df["Close"].iloc[-1])
    vwap_val  = _vwap(today_df)
    dev       = (current - vwap_val) / vwap_val
    direction = "BULL" if dev > 0 else "BEAR"
    log.info("SPY intraday: $%.2f vs VWAP $%.2f (dev %+.2f%%) → %s",
             current, vwap_val, dev * 100, direction)
    return direction


def _spy_is_bullish() -> bool:
    return _spy_direction() == "BULL"


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


def _candle_strength(df: pd.DataFrame) -> float:
    """
    Score [0,1] de calidad de vela japonesa — metodología Stockcraft.
    Body >60% del rango + cierre en tercio superior + break del máximo previo.
    """
    if len(df) < 6:
        return 0.0
    last = df.iloc[-1]
    rng  = float(last["High"] - last["Low"])
    if rng < 1e-9:
        return 0.0
    body      = abs(float(last["Close"]) - float(last["Open"])) / rng
    close_pos = (float(last["Close"]) - float(last["Low"])) / rng
    breaks_n  = int(float(last["Close"]) > float(df["High"].iloc[-6:-1].max()))
    return float(np.clip(body * 0.45 + close_pos * 0.35 + breaks_n * 0.20, 0.0, 1.0))


def _score_ticker(df: pd.DataFrame) -> tuple[float, dict]:
    """
    Score [0, 1] para un candidato DT. Retorna (score, metrics).

    Señales:
      gap_score     0.25 — gap vs cierre anterior
      orb_score     0.22 — Opening Range Breakout
      vwap_score    0.18 — precio sobre VWAP
      vol_score     0.20 — aceleración de volumen
      rsi_score     0.05 — RSI en zona neutra-alcista
      candle_score  0.10 — calidad de vela (body, close position, prev-high break)
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

    # 6. Candle quality: body ratio + close position + breaks prev-N high
    candle = _candle_strength(today_df)
    metrics["candle_score"] = round(candle, 3)

    score = (
        0.25 * gap_score
        + 0.22 * orb
        + 0.18 * vwap_score
        + 0.20 * vol_score
        + 0.05 * rsi_score
        + 0.10 * candle
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


def _score_ticker_short(df: pd.DataFrame) -> tuple[float, dict]:
    """
    Score [0, 1] para candidatos SHORT (espejo del long).
    Busca gap bajista + precio bajo VWAP + volumen + RSI sobrecomprado.
    """
    metrics: dict = {}

    today_str = df.index[-1].strftime("%Y-%m-%d")
    today_df  = df[df.index.strftime("%Y-%m-%d") == today_str]
    prev_df   = df[df.index.strftime("%Y-%m-%d") < today_str]

    if len(today_df) < 4 or len(prev_df) < 4:
        return 0.0, {}

    prev_close = float(prev_df["Close"].iloc[-1])
    today_open = float(today_df["Close"].iloc[0])
    current    = float(today_df["Close"].iloc[-1])

    # 1. Gap bajista
    gap_pct = (today_open - prev_close) / prev_close if prev_close > 0 else 0.0
    metrics["gap_pct"] = round(gap_pct, 4)
    if gap_pct > -MIN_GAP_PCT:  # necesitamos gap NEGATIVO > 1.5%
        return 0.0, metrics
    gap_score = float(np.clip(abs(gap_pct) / 0.06, 0.0, 1.0))

    # 2. Precio BAJO el VWAP (tendencia bajista intraday)
    vwap_val = _vwap(today_df)
    metrics["vwap"] = round(vwap_val, 2)
    vwap_dev = (current - vwap_val) / vwap_val if vwap_val > 0 else 0.0
    metrics["vwap_dev_pct"] = round(vwap_dev, 4)
    if vwap_dev > 0.0:
        return 0.0, metrics  # precio sobre VWAP → no short
    vwap_score = float(np.clip(abs(vwap_dev) / 0.02, 0.0, 1.0))

    # 3. Volumen acelerado
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
    vol_score = float(np.clip((vol_ratio - 1.0) / 3.0, 0.0, 1.0))

    # 4. RSI sobrevendido-neutral (26-58 para short: no sobrevendido extremo)
    rsi_val = _rsi(today_df["Close"])
    metrics["rsi"] = round(rsi_val, 1)
    if not (26.0 <= rsi_val <= 58.0):
        return 0.0, metrics
    rsi_score = float(np.clip(1.0 - abs(rsi_val - 42.0) / 16.0, 0.0, 1.0))

    # 5. ORB inverso: precio bajo el minimo del opening range
    if len(today_df) >= 3:
        orb_low = float(today_df["Low"].iloc[:2].min())
        breakout_down = (orb_low - current) / orb_low if current < orb_low else 0.0
        orb = float(np.clip(breakout_down / 0.015, 0.0, 1.0))
    else:
        orb = 0.0
    metrics["orb_score"] = round(orb, 3)

    # Candle quality for short: body large + close in LOWER third + breaks prev-N low
    if len(today_df) >= 6:
        last = today_df.iloc[-1]
        rng  = float(last["High"] - last["Low"])
        if rng > 1e-9:
            body_s     = abs(float(last["Close"]) - float(last["Open"])) / rng
            close_pos_s = (float(last["High"]) - float(last["Close"])) / rng
            breaks_low  = int(float(last["Close"]) < float(today_df["Low"].iloc[-6:-1].min()))
            candle = float(np.clip(body_s * 0.45 + close_pos_s * 0.35 + breaks_low * 0.20, 0.0, 1.0))
        else:
            candle = 0.0
    else:
        candle = 0.0
    metrics["candle_score"] = round(candle, 3)

    score = (
        0.25 * gap_score
        + 0.22 * orb
        + 0.18 * vwap_score
        + 0.20 * vol_score
        + 0.05 * rsi_score
        + 0.10 * candle
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

    Detecta automaticamente la direccion del mercado:
      SPY > VWAP → busca LONG  (gap alcista + ORB break up)
      SPY < VWAP → busca SHORT (gap bajista + ORB break down)

    Returns list of dicts con campo 'direction': 'LONG' | 'SHORT'
    """
    exclude   = exclude_tickers or set()
    candidates = []

    direction = _spy_direction()
    if direction == "FLAT":
        log.info("DT: SPY sin datos suficientes. Sin entradas.")
        return []

    log.info("DT: mercado intraday %s — escaneando candidatos %s", direction, direction)

    score_fn = _score_ticker if direction == "LONG" else _score_ticker_short

    for ticker in DT_UNIVERSE:
        if ticker in exclude:
            continue
        df = _fetch_15m(ticker)
        if df is None:
            continue

        score, metrics = score_fn(df)
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

        qty1    = qty // 2
        qty2    = qty - qty1
        notional = qty * price

        if direction == "LONG":
            sl  = round(price * (1 - DT_SL_PCT),  2)
            tp1 = round(price * (1 + DT_TP1_PCT), 2)
            tp2 = round(price * (1 + DT_TP2_PCT), 2)
        else:  # SHORT: SL arriba, TP abajo
            sl  = round(price * (1 + DT_SL_PCT),  2)
            tp1 = round(price * (1 - DT_TP1_PCT), 2)
            tp2 = round(price * (1 - DT_TP2_PCT), 2)

        candidates.append({
            "ticker":        ticker,
            "direction":     direction,
            "dt_score":      round(score, 3),
            "current_price": price,
            "qty_shares":    qty,
            "qty1":          qty1,
            "qty2":          qty2,
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
            "DT candidato %s [%s]: score=%.3f precio=$%.2f shares=%d gap=%.1f%% vol=%.1fx RSI=%.0f",
            ticker, direction, score, price, qty,
            metrics.get("gap_pct", 0.0) * 100,
            metrics.get("vol_ratio", 0.0),
            metrics.get("rsi", 0.0),
        )

    candidates.sort(key=lambda x: x["dt_score"], reverse=True)
    top = candidates[:limit]
    if top:
        best = top[0]
        log.info(
            "DT BEST [%s]: %s score=%.3f | %d shares x $%.2f = $%.0f | SL $%.2f TP1 $%.2f TP2 $%.2f",
            best["direction"], best["ticker"], best["dt_score"], best["qty_shares"],
            best["current_price"], best["notional"],
            best["stop_loss"], best["take_profit_1"], best["take_profit_2"],
        )
    else:
        log.info("DT [%s]: sin candidatos sobre umbral %.2f", direction, MIN_DT_SCORE)
    return top
