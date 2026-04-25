"""
Fuentes de datos alternativos gratuitas para el sistema de trading autónomo.

Estas fuentes complementan los datos cuantitativos (CAPM, Markowitz, técnicos)
con señales de sentimiento, macro y flujo institucional, todas sin costo y sin
necesidad de API keys.

Fuentes implementadas
---------------------
1. Fear & Greed Index (alternative.me)
   Por qué: mide el sentimiento retail del mercado en una escala 0-100.
   Extremos de miedo (< 25) son señal de compra contrarian; codicia extrema
   (> 75) sugiere cautela. Complementa el VIX como indicador de régimen.

2. FRED Yield Curve Spread 10Y-2Y (Federal Reserve de St. Louis)
   Por qué: el spread 10Y-2Y es el predictor de recesión más robusto en la
   literatura empírica. Cuando se invierte (spread < 0), históricamente precede
   una recesión 6-18 meses después. Clave para el hedge con puts SPY y para
   ajustar el sleeve LP/CP según régimen.

3. OpenInsider Cluster Buys (openinsider.com)
   Por qué: las compras de insiders (C-suite, directores) con ticket > $25k
   son señal informada. Los "cluster buys" (varios insiders comprando el mismo
   activo en una semana) tienen el mayor poder predictivo. Filtrado por los
   51 tickers del universo del sistema.

Cache y robustez
----------------
- Cada función usa un dict en memoria `_CACHE` con TTL de 1 hora.
- Si la request falla (timeout, red, parsing), retorna un valor default seguro
  y loguea el error en DEBUG para no romper el pipeline principal.
- Timeout de 10 segundos en todas las requests.
"""

from __future__ import annotations

import csv
import io
import logging
import time
from datetime import datetime, timezone
from typing import Any

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Cache en memoria: { "key": {"data": ..., "ts": float} }
# TTL_SECONDS = 3600 (1 hora)
# ─────────────────────────────────────────────────────────────────────────────
_CACHE: dict[str, dict[str, Any]] = {}
_TTL_SECONDS = 3600
_REQUEST_TIMEOUT = 10  # segundos

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


def _cache_get(key: str) -> Any | None:
    """Devuelve el valor cacheado si existe y no expiró, si no None."""
    entry = _CACHE.get(key)
    if entry and (time.monotonic() - entry["ts"]) < _TTL_SECONDS:
        return entry["data"]
    return None


def _cache_set(key: str, data: Any) -> None:
    """Guarda un valor en el cache con timestamp actual."""
    _CACHE[key] = {"data": data, "ts": time.monotonic()}


# ─────────────────────────────────────────────────────────────────────────────
# 1. Fear & Greed Index
# ─────────────────────────────────────────────────────────────────────────────

_FNG_URL = "https://api.alternative.me/fng/?limit=2"

_FNG_DEFAULT: dict[str, Any] = {
    "value": 50,
    "label": "Neutral",
    "prev_close": 50,
    "error": True,
}


def get_fear_greed() -> dict[str, Any]:
    """
    Descarga el Fear & Greed Index de alternative.me.

    Retorna
    -------
    dict con:
      - value (int): índice actual 0-100
      - label (str): clasificación ("Extreme Fear", "Fear", "Neutral",
                     "Greed", "Extreme Greed")
      - prev_close (int): valor del día anterior (para detectar cambios
                          bruscos de sentimiento)
      - error (bool): True si hubo fallo y se usa el default

    El endpoint devuelve los últimos N registros diarios en JSON:
    {"data": [{"value": "72", "value_classification": "Greed",
               "timestamp": "...", ...}, ...]}
    """
    cached = _cache_get("fear_greed")
    if cached is not None:
        return cached

    try:
        resp = requests.get(_FNG_URL, timeout=_REQUEST_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()
        payload = resp.json()
        data = payload.get("data", [])
        if not data:
            raise ValueError("Respuesta vacía de FNG API")

        current = data[0]
        prev = data[1] if len(data) > 1 else current

        result: dict[str, Any] = {
            "value": int(current["value"]),
            "label": current["value_classification"],
            "prev_close": int(prev["value"]),
            "error": False,
        }
        _cache_set("fear_greed", result)
        logger.debug("Fear & Greed: %d (%s)", result["value"], result["label"])
        return result

    except Exception as exc:
        logger.debug("get_fear_greed() falló: %s", exc)
        return _FNG_DEFAULT.copy()


# ─────────────────────────────────────────────────────────────────────────────
# 2. FRED Yield Curve Spread 10Y-2Y
# ─────────────────────────────────────────────────────────────────────────────

_FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=T10Y2Y"

_YIELD_DEFAULT: dict[str, Any] = {
    "spread_10y2y": 0.0,
    "inverted": False,
    "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    "error": True,
}


def get_yield_curve() -> dict[str, Any]:
    """
    Descarga el spread 10Y-2Y desde FRED (Federal Reserve de St. Louis).

    El CSV tiene dos columnas: DATE, T10Y2Y.
    Las filas con valor "." (sin dato) se saltean; se toma la última
    observación válida.

    Retorna
    -------
    dict con:
      - spread_10y2y (float): spread en puntos porcentuales (ej: 0.45 = 45 bps)
      - inverted (bool): True si spread < 0 (señal de recesión potencial)
      - date (str): fecha de la última observación en formato YYYY-MM-DD
      - error (bool): True si hubo fallo y se usa el default

    Interpretación para el sistema:
      - spread > 1.0  → curva empinada, régimen de expansión, favorecer β alta
      - spread 0-1.0  → curva plana, cautela moderada
      - spread < 0    → curva invertida, activar hedge puts SPY si VIX > 20
    """
    cached = _cache_get("yield_curve")
    if cached is not None:
        return cached

    try:
        resp = requests.get(_FRED_URL, timeout=_REQUEST_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()

        reader = csv.reader(io.StringIO(resp.text))
        next(reader, None)  # saltar header

        last_date = ""
        last_value = None
        for row in reader:
            if len(row) < 2:
                continue
            date_str, val_str = row[0].strip(), row[1].strip()
            if val_str == "." or val_str == "":
                continue
            try:
                last_value = float(val_str)
                last_date = date_str
            except ValueError:
                continue

        if last_value is None:
            raise ValueError("No se encontraron datos válidos en el CSV de FRED")

        result: dict[str, Any] = {
            "spread_10y2y": round(last_value, 4),
            "inverted": last_value < 0,
            "date": last_date,
            "error": False,
        }
        _cache_set("yield_curve", result)
        logger.debug(
            "Yield curve: spread=%.2f, invertida=%s (fecha %s)",
            result["spread_10y2y"],
            result["inverted"],
            result["date"],
        )
        return result

    except Exception as exc:
        logger.debug("get_yield_curve() falló: %s", exc)
        return _YIELD_DEFAULT.copy()


# ─────────────────────────────────────────────────────────────────────────────
# 3. OpenInsider Cluster Buys
# ─────────────────────────────────────────────────────────────────────────────

_OPENINSIDER_URL = (
    "https://openinsider.com/screener"
    "?s=&o=&pl=1&ph=&ll=&lh=&fd=7&fdr=&td=0&tdr=&fdlyl=&fdlyh="
    "&daysago=7&xp=1&xs=1&vl=25&vh=&ocl=&och=&sic1=-1&sicl=100&sich=9999"
    "&grp=0&nfl=&nfh=&nil=&nih=&nol=&noh=&v2l=&v2h=&oc2l=&oc2h="
    "&sortcol=0&cnt=40&action=1"
)

_INSIDER_DEFAULT: list[dict[str, Any]] = []


def get_insider_buys(tickers: list[str]) -> list[dict[str, Any]]:
    """
    Scraping de compras de insiders de OpenInsider (últimos 7 días, > $25k).

    Parsea la tabla HTML con BeautifulSoup, sin pandas. Filtra las filas
    cuyo ticker está en la lista proporcionada.

    Parámetros
    ----------
    tickers : list[str]
        Lista de tickers a filtrar (ej: la lista del universo del sistema).
        Si está vacía, retorna todas las filas sin filtrar.

    Retorna
    -------
    list[dict] con campos:
      - ticker (str): símbolo bursátil
      - insider (str): nombre del insider (CEO, CFO, director, etc.)
      - title (str): cargo del insider
      - transaction_date (str): fecha de la transacción (YYYY-MM-DD)
      - shares (int): cantidad de acciones compradas
      - value_usd (int): valor aproximado en USD de la transacción
      - own_change_pct (str): cambio porcentual en tenencia del insider

    Por qué es señal relevante:
    Los insiders solo compran si creen que el precio subirá. Múltiples insiders
    comprando el mismo activo en una semana ("cluster buy") es una de las señales
    con mejor poder predictivo documentado en la literatura académica.
    """
    cache_key = "insider_buys"
    cached = _cache_get(cache_key)
    if cached is not None:
        raw: list[dict[str, Any]] = cached
    else:
        try:
            resp = requests.get(
                _OPENINSIDER_URL,
                timeout=_REQUEST_TIMEOUT,
                headers=_HEADERS,
            )
            resp.raise_for_status()
            raw = _parse_openinsider_table(resp.text)
            _cache_set(cache_key, raw)
            logger.debug("OpenInsider: %d filas descargadas", len(raw))
        except Exception as exc:
            logger.debug("get_insider_buys() falló al descargar: %s", exc)
            return _INSIDER_DEFAULT.copy()

    # Filtrar por tickers del universo (normalizar a mayúsculas)
    if not tickers:
        return raw

    ticker_set = {t.upper() for t in tickers}
    filtered = [row for row in raw if row.get("ticker", "").upper() in ticker_set]
    return filtered


def _parse_openinsider_table(html: str) -> list[dict[str, Any]]:
    """
    Parsea la tabla principal de resultados de OpenInsider.

    La tabla tiene clase CSS 'tinytable' (la primera tabla grande de resultados).
    Columnas esperadas en el orden del screener de OpenInsider:
    X | Filing Date | Trade Date | Ticker | Company | Insider Name |
    Title | Trade Type | Price | Qty | Owned | ΔOwn | Value

    Retorna lista de dicts con los campos normalizados.
    """
    soup = BeautifulSoup(html, "html.parser")

    # OpenInsider usa la clase 'tinytable' para la tabla de resultados
    table = soup.find("table", {"class": "tinytable"})
    if table is None:
        # Intentar fallback con cualquier tabla grande
        tables = soup.find_all("table")
        # Tomar la más grande por número de filas
        table = max(tables, key=lambda t: len(t.find_all("tr")), default=None)

    if table is None:
        logger.debug("OpenInsider: no se encontró tabla en el HTML")
        return []

    rows = table.find_all("tr")
    results: list[dict[str, Any]] = []

    for row in rows[1:]:  # saltar header
        cells = row.find_all("td")
        if len(cells) < 13:
            continue

        try:
            # Columna 3 = Ticker, columna 2 = Trade Date, columna 5 = Insider Name
            # columna 6 = Title, columna 9 = Qty, columna 12 = Value
            ticker_cell = cells[3].get_text(strip=True)
            trade_date = cells[2].get_text(strip=True)
            insider_name = cells[5].get_text(strip=True)
            title = cells[6].get_text(strip=True)
            qty_raw = cells[9].get_text(strip=True).replace(",", "").replace("+", "")
            value_raw = cells[12].get_text(strip=True).replace(",", "").replace("$", "").replace("+", "")
            own_change = cells[11].get_text(strip=True)

            # Normalizar fecha: OpenInsider usa formato MM/DD/YYYY o YYYY-MM-DD
            trade_date_norm = _normalize_date(trade_date)

            shares = int(float(qty_raw)) if qty_raw else 0
            value_usd = int(float(value_raw)) if value_raw else 0

            # Solo incluir compras con valor positivo (excluir ejercicios de opciones
            # y ventas que puedan colarse con valores negativos)
            if value_usd <= 0 or shares <= 0:
                continue

            results.append(
                {
                    "ticker": ticker_cell.upper(),
                    "insider": insider_name,
                    "title": title,
                    "transaction_date": trade_date_norm,
                    "shares": shares,
                    "value_usd": value_usd,
                    "own_change_pct": own_change,
                }
            )
        except (ValueError, IndexError) as exc:
            logger.debug("OpenInsider: fila ignorada por error de parsing: %s", exc)
            continue

    return results


def _normalize_date(date_str: str) -> str:
    """Convierte una fecha de OpenInsider a formato YYYY-MM-DD."""
    date_str = date_str.strip()
    # Intentar formatos comunes
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Si no se puede parsear, devolver tal cual
    return date_str


# ─────────────────────────────────────────────────────────────────────────────
# Función unificadora
# ─────────────────────────────────────────────────────────────────────────────


def get_all_alternative_data(tickers: list[str]) -> dict[str, Any]:
    """
    Llama a las tres fuentes y devuelve un dict unificado.

    Parámetros
    ----------
    tickers : list[str]
        Lista de tickers del universo del sistema. Se usa para filtrar
        las compras de insiders relevantes.

    Retorna
    -------
    dict con estructura:
    {
        "fear_greed": {
            "value": int,          # 0-100
            "label": str,          # "Extreme Fear" … "Extreme Greed"
            "prev_close": int,     # valor día anterior
            "error": bool
        },
        "yield_curve": {
            "spread_10y2y": float, # en puntos porcentuales
            "inverted": bool,      # True → señal recesión
            "date": str,           # última observación YYYY-MM-DD
            "error": bool
        },
        "insider_buys": [          # lista vacía si no hay matches
            {
                "ticker": str,
                "insider": str,
                "title": str,
                "transaction_date": str,
                "shares": int,
                "value_usd": int,
                "own_change_pct": str
            },
            ...
        ],
        "timestamp": str           # ISO UTC del momento de consulta
    }

    Interpretación rápida para el pipeline:
      - fear_greed.value < 25 → mercado en pánico, señal de compra contrarian
      - fear_greed.value > 75 → euforia, reducir exposición
      - yield_curve.inverted == True → considerar activar hedge puts SPY
      - insider_buys no vacío → refuerza la tesis alcista de los tickers listados
    """
    fng = get_fear_greed()
    yc = get_yield_curve()
    insiders = get_insider_buys(tickers)

    return {
        "fear_greed": fng,
        "yield_curve": yc,
        "insider_buys": insiders,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
