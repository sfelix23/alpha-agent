"""
Sector Rotation — detecta qué sectores tienen momentum y devuelve
un multiplicador de score para cada ticker según su sector.

Lógica:
  1. Descarga 3 meses de precios de ETFs sectoriales.
  2. Calcula retorno 1m y 3m de cada sector.
  3. Rankea sectores por momentum combinado.
  4. Devuelve boost_map: {ticker → multiplicador} donde los tickers
     de sectores top reciben hasta +40% de boost en su score.

ETFs usados como proxy de sector:
  XLK → Tech     XLE → Energy    XLI → Industrials  XLF → Financials
  XLV → Health   XLB → Materials XLC → Comm          ITA → Defense
  GDX → GoldMin  GLD → Gold      IBIT → Crypto       QQQ → Mega-cap

El boost se integra en scoring.py para LP y CP.
"""

from __future__ import annotations

import logging
from datetime import date

import numpy as np
import pandas as pd
import yfinance as yf

from alpha_agent.config import PATHS, SECTOR_MAP

logger = logging.getLogger(__name__)

# ETF → etiqueta de sector (mismas que en SECTOR_MAP)
SECTOR_ETFS: dict[str, str] = {
    "XLK":  "Tech",
    "XLE":  "Energy",
    "XLI":  "Industrials",
    "XLF":  "Financials",
    "XLV":  "Healthcare",
    "XLB":  "Materials",
    "XLC":  "Comm",
    "ITA":  "Defense",
    "GDX":  "GoldMiners",
    "GLD":  "ETF",          # oro (ya en SECTOR_MAP como ETF)
    "QQQ":  "ETF",
}

# Cuánto boost máximo recibe el sector #1 (el #2 recibe la mitad, el resto 0)
MAX_BOOST = 0.35   # +35% al score de los tickers del sector más fuerte


def _cache_file() -> "Path":
    from pathlib import Path
    return PATHS.cache_dir / f"sector_rotation_{date.today().isoformat()}.parquet"


def fetch_sector_momentum() -> dict[str, float]:
    """
    Descarga retornos de ETFs sectoriales y devuelve
    {sector_label → score_momentum} (mayor = mejor).

    Usa cache diario para no re-descargar.
    """
    cache = _cache_file()
    if cache.exists():
        try:
            return pd.read_parquet(cache).squeeze().to_dict()
        except Exception:
            pass

    tickers = list(SECTOR_ETFS.keys())
    try:
        raw = yf.download(tickers, period="4mo", interval="1d",
                          auto_adjust=True, progress=False)
        if isinstance(raw.columns, pd.MultiIndex):
            closes = raw["Close"]
        else:
            closes = raw[["Close"]] if "Close" in raw.columns else raw
    except Exception as exc:
        logger.warning("sector_rotation: fallo descarga ETFs (%s) — sin boost", exc)
        return {}

    scores: dict[str, float] = {}
    for etf, sector in SECTOR_ETFS.items():
        if etf not in closes.columns:
            continue
        s = closes[etf].dropna()
        if len(s) < 22:
            continue
        ret_1m = float(s.iloc[-1] / s.iloc[-21] - 1)
        ret_3m = float(s.iloc[-1] / s.iloc[-63] - 1) if len(s) >= 64 else ret_1m
        # Momentum combinado: 40% del último mes + 60% de los últimos 3 meses
        momentum = 0.4 * ret_1m + 0.6 * ret_3m
        # Si ya hay otro ETF para el mismo sector, promediamos
        if sector in scores:
            scores[sector] = (scores[sector] + momentum) / 2
        else:
            scores[sector] = momentum

    try:
        pd.Series(scores).to_frame().to_parquet(cache)
    except Exception:
        pass

    logger.info("Sector momentum: %s",
                {k: f"{v*100:+.1f}%" for k, v in sorted(scores.items(), key=lambda x: -x[1])})
    return scores


def build_sector_boost(tickers: list[str]) -> dict[str, float]:
    """
    Devuelve {ticker → boost_multiplier} para todos los tickers dados.

    El sector con mayor momentum recibe un boost de MAX_BOOST (35%).
    El segundo sector recibe MAX_BOOST / 2 (17.5%).
    El resto recibe 0.
    """
    momentum = fetch_sector_momentum()
    if not momentum:
        return {t: 1.0 for t in tickers}

    ranked = sorted(momentum.items(), key=lambda x: -x[1])
    top_sectors = [s for s, _ in ranked[:2] if ranked[0][1] > 0]

    boost_map: dict[str, float] = {}
    for ticker in tickers:
        sector = SECTOR_MAP.get(ticker, "Other")
        if sector == top_sectors[0] if top_sectors else None:
            boost_map[ticker] = 1.0 + MAX_BOOST
        elif len(top_sectors) > 1 and sector == top_sectors[1]:
            boost_map[ticker] = 1.0 + MAX_BOOST / 2
        else:
            boost_map[ticker] = 1.0

    return boost_map


def get_top_sectors(n: int = 3) -> list[tuple[str, float]]:
    """Devuelve los N sectores con mejor momentum. Para el WhatsApp brief."""
    momentum = fetch_sector_momentum()
    return sorted(momentum.items(), key=lambda x: -x[1])[:n]
