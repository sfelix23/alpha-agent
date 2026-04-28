"""
Polymarket — probabilidades de mercados de prediccion.

API publica CLOB: https://clob.polymarket.com/markets
Los precios representan probabilidades reales (dinero en juego).

Mapea mercados relevantes a señales macro para el MacroAgent:
  fed_cut        → politica monetaria → afecta yields, DXY
  recession      → riesgo sistemico   → afecta beta alto
  oil_spike      → energia            → afecta XOM, CVX, SLB
  tariff_escalation → geopolitico     → afecta TSLA, AAPL, AMZN
  vix_spike      → volatilidad        → afecta sizing
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
import json

log = logging.getLogger(__name__)

# Palabras clave para buscar mercados relevantes
_KEYWORDS = {
    "fed_cut":           ["fed", "rate cut", "federal reserve", "fomc", "interest rate"],
    "recession":         ["recession", "gdp contraction", "economic downturn"],
    "oil_spike":         ["oil price", "crude", "opec", "wti"],
    "tariff_escalation": ["tariff", "trade war", "china tariff", "import duty"],
    "vix_spike":         ["vix", "volatility", "market crash", "stock crash"],
    "geopolitical":      ["war", "iran", "ormuz", "middle east", "israel", "ukraine"],
}

_CACHE_TTL = 3600  # 1h
_CACHE_PATH = Path(__file__).parent.parent.parent / "signals" / "polymarket_cache.json"


def _load_cache() -> dict | None:
    if _CACHE_PATH.exists():
        try:
            data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
            if time.time() - data.get("ts", 0) < _CACHE_TTL:
                return data.get("signals")
        except Exception:
            pass
    return None


def _save_cache(signals: dict) -> None:
    try:
        _CACHE_PATH.parent.mkdir(exist_ok=True)
        _CACHE_PATH.write_text(
            json.dumps({"ts": time.time(), "signals": signals}), encoding="utf-8"
        )
    except Exception:
        pass


def _fetch_raw(limit: int = 500) -> list[dict]:
    import urllib.request
    url = f"https://clob.polymarket.com/markets?limit={limit}&active=true"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    return data.get("data", data) if isinstance(data, dict) else data


def _match_keyword(question: str, keywords: list[str]) -> bool:
    q = question.lower()
    return any(k in q for k in keywords)


def fetch_polymarket_signals() -> dict[str, float]:
    """
    Retorna dict de señales macro con probabilidades [0, 1].

    Ejemplo:
        {"fed_cut": 0.23, "tariff_escalation": 0.81, "recession": 0.34, ...}

    Falla silenciosamente si la API no está disponible.
    """
    cached = _load_cache()
    if cached is not None:
        log.debug("Polymarket: cache hit (%d señales)", len(cached))
        return cached

    try:
        markets = _fetch_raw(limit=500)
    except Exception as e:
        log.warning("Polymarket API no disponible: %s", e)
        return {}

    signals: dict[str, list[float]] = {k: [] for k in _KEYWORDS}

    for m in markets:
        question = m.get("question", "") or m.get("title", "") or ""
        # El precio en Polymarket CLOB es 0-1 (probabilidad)
        price = None
        for field in ("last_trade_price", "best_ask", "best_bid", "price"):
            val = m.get(field)
            if val is not None:
                try:
                    price = float(val)
                    if 0.0 < price < 1.0:
                        break
                except Exception:
                    pass

        if price is None:
            continue

        for signal_key, keywords in _KEYWORDS.items():
            if _match_keyword(question, keywords):
                signals[signal_key].append(price)

    result: dict[str, float] = {}
    for key, prices in signals.items():
        if prices:
            result[key] = round(sum(prices) / len(prices), 3)

    if result:
        log.info("Polymarket: %d señales obtenidas — %s", len(result),
                 " | ".join(f"{k}={v:.0%}" for k, v in result.items()))
        _save_cache(result)
    else:
        log.debug("Polymarket: sin mercados relevantes encontrados")

    return result
