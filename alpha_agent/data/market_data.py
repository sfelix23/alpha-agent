"""
Descarga y cacheo de precios históricos vía yfinance.

Mejoras vs script original:
- Descarga TODO el universo en una sola llamada (yf.download multi-ticker).
- Cache local en pickle por (kind, period, fecha) → sin dependencia de pyarrow.
- Logging estructurado en vez de prints sueltos.
- Filtra silenciosamente tickers delistados (no rompe el pipeline).
- Devuelve un DataFrame ancho (columnas = tickers) listo para los analytics.
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import pickle
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import date
from pathlib import Path

import pandas as pd
import yfinance as yf

from alpha_agent.config import ACTIVOS, BENCHMARK_TICKER, PARAMS, PATHS

logger = logging.getLogger(__name__)

# Timeout duro para yfinance — sin esto, una red lenta puede colgar el analyst
# bloqueando el pipeline completo de Cloud Run (que tiene timeout de 30min).
_YF_TIMEOUT_SECONDS = 45.0

# Silenciar yfinance/yfinance-cache verbosity — sus errores de delisted tickers
# son ruido: el código ya los filtra por min_obs.
for noisy in ("yfinance", "yfinance.cache"):
    logging.getLogger(noisy).setLevel(logging.CRITICAL)


@contextlib.contextmanager
def _silence_stderr():
    """Contexto para atrapar stderr — yfinance imprime directo al fd, no al logger."""
    with open(os.devnull, "w") as devnull:
        with contextlib.redirect_stderr(devnull):
            yield


def _cache_path(kind: str) -> Path:
    """Cache key incluye fecha — invalida automáticamente día a día."""
    today = date.today().isoformat()
    return PATHS.cache_dir / f"{kind}_{PARAMS.history_period}_{today}.pkl"


def _atomic_pickle_write(df: pd.DataFrame, cache: Path) -> None:
    """Escribe el pickle via tempfile + os.replace para evitar corrupción.

    Si el proceso muere a mitad de `to_pickle`, el archivo final queda
    truncado y el próximo read lanza UnpicklingError. Con atomic write, el
    archivo viejo se mantiene hasta que el nuevo está completo.
    """
    tmp = cache.with_suffix(cache.suffix + ".tmp")
    df.to_pickle(tmp)
    os.replace(tmp, cache)


def _yf_download_with_timeout(**kwargs) -> pd.DataFrame:
    """Wrapper de yf.download con timeout duro para no colgar el pipeline."""
    with ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(lambda: yf.download(**kwargs))
        return future.result(timeout=_YF_TIMEOUT_SECONDS)


def _download_close(tickers: list[str], label: str) -> pd.DataFrame:
    """Descarga cierres ajustados de una lista de tickers en una sola llamada."""
    cache = _cache_path(label)
    if cache.exists():
        try:
            df = pd.read_pickle(cache)
            logger.info("Cache hit: %s", cache.name)
            return df
        except (pickle.UnpicklingError, EOFError, OSError, AttributeError) as e:
            # Específicos: pickle corrupto (proceso muerto durante write), FS error,
            # versión incompatible. No swallowear errores reales (MemoryError etc.).
            logger.warning("Cache corrupto %s (%s). Borrando y re-descargando.", cache.name, e)
            try:
                cache.unlink()
            except OSError:
                pass

    logger.info("Descargando %d tickers desde Yahoo Finance…", len(tickers))
    try:
        with _silence_stderr():
            raw = _yf_download_with_timeout(
                tickers=tickers,
                period=PARAMS.history_period,
                interval=PARAMS.history_interval,
                auto_adjust=True,
                progress=False,
                group_by="column",
                threads=True,
            )
    except FuturesTimeout:
        logger.warning("yfinance timeout (%ss) — usando cache stale si existe", _YF_TIMEOUT_SECONDS)
        if cache.exists():
            try:
                return pd.read_pickle(cache)
            except (pickle.UnpicklingError, EOFError, OSError):
                pass
        raise RuntimeError(f"yfinance timeout y sin cache fallback para {label}")

    # yfinance devuelve MultiIndex cuando hay >1 ticker
    if isinstance(raw.columns, pd.MultiIndex):
        close = raw["Close"].copy()
    else:
        close = raw[["Close"]].rename(columns={"Close": tickers[0]})

    close = close.dropna(how="all")
    # Guardar cache solo si la cobertura es suficiente — evitar cachear datos parciales
    # (yfinance puede retornar menos tickers si algunos fallan temporalmente)
    expected_n = len(tickers)
    actual_n = close.shape[1]
    if actual_n < max(1, expected_n * 0.75):
        logger.warning(
            "Cache skip: solo %d/%d tickers descargados (<75%%) — datos parciales, no cachear",
            actual_n, expected_n,
        )
    else:
        _atomic_pickle_write(close, cache)
    logger.info("Guardado en cache: %s (%d filas, %d activos válidos)", cache.name, len(close), close.shape[1])
    return close


def _download_ohlc(ticker: str) -> pd.DataFrame | None:
    """Descarga OHLC de un solo ticker — necesario para indicadores técnicos (ATR)."""
    cache = PATHS.cache_dir / f"ohlc_{ticker}_{PARAMS.history_period}_{date.today().isoformat()}.pkl"
    if cache.exists():
        try:
            return pd.read_pickle(cache)
        except (pickle.UnpicklingError, EOFError, OSError, AttributeError) as e:
            logger.debug("Cache OHLC corrupto %s (%s). Borrando.", cache.name, e)
            try:
                cache.unlink()
            except OSError:
                pass

    try:
        with _silence_stderr():
            df = _yf_download_with_timeout(
                tickers=ticker,
                period=PARAMS.history_period,
                interval=PARAMS.history_interval,
                auto_adjust=True,
                progress=False,
            )
    except FuturesTimeout:
        logger.debug("yfinance OHLC timeout para %s", ticker)
        return None
    except Exception as e:
        # Single-ticker download falla con varios tipos (HTTPError, JSONDecodeError, etc.).
        # No vale la pena enumerar todos para un best-effort fetch que tiene cache fallback.
        logger.debug("yfinance falló para %s: %s", ticker, e)
        return None

    if df is None or df.empty:
        return None

    # aplanar MultiIndex si yfinance lo devuelve
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    needed = ["Open", "High", "Low", "Close", "Volume"]
    if not all(c in df.columns for c in needed):
        return None

    df = df[needed].dropna()
    if len(df) < PARAMS.min_obs:
        return None

    _atomic_pickle_write(df, cache)
    return df


def download_universe(
    extra_tickers: list[str] | None = None,
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    """
    Descarga todo el universo definido en config.ACTIVOS, más cualquier
    ticker adicional descubierto por el Discovery Agent.

    Args:
        extra_tickers: lista de tickers extra (del screener), puede ser None.

    Returns:
        closes: DataFrame de cierres ajustados (index=fecha, cols=ticker).
        ohlc:   dict {ticker: DataFrame OHLCV} para indicadores técnicos.
    """
    tickers = list(ACTIVOS.values())
    if extra_tickers:
        new = [t for t in extra_tickers if t not in tickers]
        if new:
            logger.info("Discovery: agregando %d tickers al universo: %s", len(new), new)
            tickers = tickers + new
    if BENCHMARK_TICKER not in tickers:
        tickers = [BENCHMARK_TICKER, *tickers]

    closes = _download_close(tickers, "universo")

    # Filtrar activos con muy poca historia
    valid_cols = [c for c in closes.columns if closes[c].dropna().shape[0] >= PARAMS.min_obs]
    dropped = set(closes.columns) - set(valid_cols)
    if dropped:
        logger.warning("Activos descartados por insuficiente historia: %s", sorted(dropped))
    closes = closes[valid_cols]

    # OHLC individual solo para los tickers que sí tienen historia válida
    ohlc: dict[str, pd.DataFrame] = {}
    for t in valid_cols:
        df = _download_ohlc(t)
        if df is not None and len(df) >= PARAMS.min_obs:
            ohlc[t] = df

    return closes, ohlc


def load_benchmark(closes: pd.DataFrame) -> pd.Series:
    """Devuelve la serie de cierres del benchmark (SPY) ya cargada en `closes`."""
    if BENCHMARK_TICKER not in closes.columns:
        raise KeyError(f"Benchmark {BENCHMARK_TICKER} no está en el set de datos.")
    return closes[BENCHMARK_TICKER].dropna()
