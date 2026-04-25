"""
Discovery Agent — busca activos con potencial fuera del universo fijo.

Pipeline:
  1. Escanea ~200 candidatos de sectores clave (IA, defensa, materiales, etc.)
  2. Calcula indicadores técnicos rápidos para cada uno.
  3. Puntúa con 4 criterios: momentum, volumen, breakout, distancia a máximos.
  4. Los top N pasan por Claude (si disponible) para validar el catalizador noticioso.
  5. Devuelve lista de tickers recomendados para agregar al universo del día.

El analyst los agrega a su universo dinámicamente antes de correr CAPM + Markowitz.
"""

from __future__ import annotations

import logging
import os
from datetime import date

import numpy as np
import pandas as pd
import yfinance as yf

from alpha_agent.config import ACTIVOS, PATHS

logger = logging.getLogger(__name__)

# ── Universo extendido de candidatos ─────────────────────────────────────────
# Activos de alta convicción temática que NO están en el universo fijo.
# Organizados por tema para facilitar el mantenimiento.

DISCOVERY_CANDIDATES: dict[str, list[str]] = {
    # IA / Semiconductores — el sector con mayor momentum estructural
    "AI_Infrastructure": [
        "SMCI", "CRDO", "ARM", "MRVL", "ANET", "DELL", "HPE",
        "SNOW", "DDOG", "NET", "MDB", "ZS", "PANW", "OKTA",
        "APP", "RBRK", "BBAI", "SOUN", "ARCT",
    ],
    # Defensa / Geopolítica — beneficiarios de tensiones globales
    "Defense_Asymmetric": [
        "HII", "TXT", "LDOS", "KTOS", "RCAT", "BWXT", "CW",
        "AXON", "VVX", "DRS", "ACHR",
    ],
    # Nuclear / Uranio — megatendencia energía limpia + AI data centers
    "Nuclear_Power": [
        "LEU", "UEC", "UUUU", "NXE", "DNN", "CEG", "VST", "ETR",
        "OKLO", "SMR", "NNE",
    ],
    # Materiales críticos — litio, cobre, plata, oro royalties
    "Critical_Minerals": [
        "SCCO", "CDE", "PAAS", "AG", "WPM", "AEM", "TFPM",
        "MP", "NOVN", "ALTM",
    ],
    # Petróleo y gas — pequeñas productoras con alto beta a WTI
    "Energy_High_Beta": [
        "OXY", "DVN", "MRO", "FANG", "SM", "PR", "CIVI",
        "MTDR", "CHRD", "ERX",  # ERX = ETF leveraged energy 2x
    ],
    # FinTech / Cripto / Digital — alto beta a apetito de riesgo
    "FinTech_Digital": [
        "SQ", "PYPL", "NU", "AFRM", "SOFI", "HOOD",
        "MSTR", "RIOT", "MARA", "HUT", "CLSK",
    ],
    # Biotech — movimientos binarios grandes (FDA, ensayos)
    "Biotech_Catalyst": [
        "MRNA", "REGN", "VRTX", "ABBV", "GILD", "BIIB",
        "RXRX", "BEAM", "EDIT", "NTLA", "CRSP",
    ],
    # Latinoamérica / Emergentes — donde el usuario tiene edge local
    "LatAm_Emerging": [
        "SE", "NU", "PDD", "GRAB", "BEKE",
        "CEPU", "SUPV", "BBAR", "AGRO",  # más ADRs argentinos
        "BTGPACTUAL", "ITUB", "BBD",     # Brasil
    ],
    # Semiconductores extendidos — toda la cadena de suministro de AI chips
    "Semis_Extended": [
        "ON", "MCHP", "SWKS", "QCOM", "MPWR", "LRCX", "KLAC",
        "ONTO", "WOLF", "AMBA", "SLAB",
    ],
    # Small/Mid cap momentum — alta convicción institucional reciente
    "Momentum_Plays": [
        "CELH", "DUOL", "GLBE", "TMDX", "TBLA", "INSP",
        "ASTS", "RKLB", "LUNR", "JOBY",  # space/new tech
    ],
    # Commodities / Inflación — protección y upside en ciclo inflacionario
    "Commodities_Cycle": [
        "CF", "MOS", "NTR", "IPI",   # fertilizantes
        "X", "CLF", "STLD",           # acero
        "AA", "CENX",                 # aluminio
    ],
}

FIXED_UNIVERSE = set(ACTIVOS.values())
MAX_CANDIDATES_TO_RETURN = 6   # concentrado: 6 candidatos bien filtrados > 15 mediocres


def _flat_candidates() -> list[str]:
    """Lista plana de candidatos, excluyendo los ya en el universo fijo."""
    all_cands = []
    for tickers in DISCOVERY_CANDIDATES.values():
        for t in tickers:
            if t not in FIXED_UNIVERSE:
                all_cands.append(t)
    return list(dict.fromkeys(all_cands))  # deduplica conservando orden


def _cache_file() -> "Path":
    from pathlib import Path
    return PATHS.cache_dir / f"discovery_{date.today().isoformat()}.parquet"


def _score_candidate(row: pd.Series) -> float:
    """
    Score técnico simple (0-100) para filtrar candidatos antes de Claude.

    Criterios:
      - Momentum 1m (30%): retorno reciente
      - Momentum 3m (20%): tendencia más larga
      - Volume ratio (25%): convicción del movimiento
      - Breakout (15%): precio en máximos con volumen
      - RSI no sobrecomprado (10%): no comprar en el pico
    """
    score = 0.0

    ret_1m = row.get("ret_1m", 0) or 0
    ret_3m = row.get("ret_3m", 0) or 0
    vol_ratio = row.get("vol_ratio", 1.0) or 1.0
    breakout = row.get("breakout", 0) or 0
    rsi = row.get("rsi", 50) or 50

    # momentum 1m: +30 pts si +15%, proporcional
    score += min(30, max(-15, ret_1m * 200))

    # momentum 3m: +20 pts si +20%
    score += min(20, max(-10, ret_3m * 100))

    # volumen: +25 pts si vol_ratio > 2
    score += min(25, (vol_ratio - 1) * 15)

    # breakout: binario +15 pts
    score += 15 * breakout

    # RSI: penalizar sobrecompra fuerte
    if rsi > 75:
        score -= 10
    elif rsi < 40:
        score += 5   # sobreventa = oportunidad

    return score


def _fetch_technicals(tickers: list[str]) -> pd.DataFrame:
    """Descarga OHLCV de 3 meses y calcula técnicos básicos para cada ticker."""
    cache = _cache_file()
    if cache.exists():
        try:
            return pd.read_parquet(cache)
        except Exception:
            pass

    if not tickers:
        return pd.DataFrame()

    try:
        raw = yf.download(
            tickers, period="6mo", interval="1d",
            auto_adjust=True, progress=False, threads=True
        )
    except Exception as exc:
        logger.warning("Discovery: error descargando datos (%s)", exc)
        return pd.DataFrame()

    if isinstance(raw.columns, pd.MultiIndex):
        closes  = raw["Close"]
        volumes = raw["Volume"] if "Volume" in raw.columns else pd.DataFrame()
        highs   = raw["High"]   if "High"   in raw.columns else pd.DataFrame()
    else:
        logger.warning("Discovery: formato inesperado de yfinance")
        return pd.DataFrame()

    rows = []
    for ticker in tickers:
        if ticker not in closes.columns:
            continue
        close = closes[ticker].dropna()
        if len(close) < 30:
            continue

        price = float(close.iloc[-1])
        ret_1m = float(close.iloc[-1] / close.iloc[-21] - 1) if len(close) >= 22 else np.nan
        ret_3m = float(close.iloc[-1] / close.iloc[-63] - 1) if len(close) >= 64 else np.nan

        # RSI nativo simple
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = -delta.clip(upper=0).rolling(14).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi_series = 100 - 100 / (1 + rs)
        rsi_val = float(rsi_series.iloc[-1]) if not np.isnan(rsi_series.iloc[-1]) else 50.0

        # Volumen
        vol_ratio = np.nan
        if ticker in volumes.columns:
            vol = volumes[ticker].dropna()
            if len(vol) >= 21:
                avg = float(vol.rolling(20).mean().iloc[-1])
                vol_ratio = float(vol.iloc[-1]) / avg if avg > 0 else 1.0

        # Breakout: precio en máx 20d con volumen alto
        high_20d = float(close.tail(20).max())
        breakout = int(price >= high_20d * 0.99 and (vol_ratio or 0) > 1.5)

        rows.append({
            "ticker": ticker, "price": price,
            "ret_1m": ret_1m, "ret_3m": ret_3m,
            "rsi": rsi_val, "vol_ratio": vol_ratio,
            "breakout": breakout,
        })

    result = pd.DataFrame(rows).set_index("ticker")
    try:
        result.to_parquet(cache)
    except Exception:
        pass
    return result


def _validate_with_claude(ticker: str, score: float, ret_1m: float, ret_3m: float) -> bool:
    """
    Pregunta a Claude si el ticker tiene un catalizador real.
    Devuelve True si Claude considera que vale incluirlo.
    Si Claude no está disponible, acepta cualquier score > 40.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return score > 40

    try:
        import anthropic, json, re
        client = anthropic.Anthropic(api_key=api_key)

        # Buscar noticias rápidas con yfinance
        try:
            news_items = yf.Ticker(ticker).news or []
            headlines = [n.get("title", "") for n in news_items[:4] if n.get("title")]
        except Exception:
            headlines = []

        news_text = "\n".join(f"- {h}" for h in headlines) if headlines else "Sin noticias disponibles."

        prompt = f"""Evaluate this stock as a SHORT-TERM trade candidate (1-4 weeks).

Ticker: {ticker}
Momentum 1 month: {ret_1m*100:+.1f}%
Momentum 3 months: {ret_3m*100:+.1f}%
Technical score: {score:.0f}/100

Recent news:
{news_text}

Should we add this to our portfolio watchlist?
- YES if: real catalyst (earnings beat, new contract, sector tailwind, technical breakout with volume)
- NO if: just noise, no clear catalyst, or fundamental deterioration

Reply JSON only: {{"include": true/false, "reason": "≤10 words"}}"""

        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=60,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text
        match = re.search(r"\{.*?\}", text, re.DOTALL)
        if match:
            result = json.loads(match.group())
            include = bool(result.get("include", False))
            logger.info("Claude sobre %s: %s — %s", ticker,
                        "INCLUDE" if include else "SKIP", result.get("reason", ""))
            return include
    except Exception as exc:
        logger.debug("Claude discovery failed for %s: %s", ticker, exc)

    return score > 40


def run_discovery(max_new: int = MAX_CANDIDATES_TO_RETURN) -> list[str]:
    """
    Punto de entrada principal.

    Retorna lista de tickers nuevos a agregar al universo del día.
    """
    candidates = _flat_candidates()
    logger.info("Discovery: evaluando %d candidatos externos", len(candidates))

    technicals = _fetch_technicals(candidates)
    if technicals.empty:
        logger.warning("Discovery: sin datos técnicos, saltando")
        return []

    # Score inicial
    technicals["score"] = technicals.apply(_score_candidate, axis=1)
    ranked = technicals.sort_values("score", ascending=False)

    # Pre-filtro: top 15 técnicos antes de llamar a Claude
    top_tech = ranked.head(15)
    logger.info("Discovery top técnico: %s", top_tech.head(5).index.tolist())

    selected = []
    for ticker, row in top_tech.iterrows():
        if len(selected) >= max_new:
            break
        score   = float(row["score"])
        ret_1m  = float(row.get("ret_1m", 0) or 0)
        ret_3m  = float(row.get("ret_3m", 0) or 0)

        if _validate_with_claude(str(ticker), score, ret_1m, ret_3m):
            selected.append(str(ticker))
            logger.info("  ✅ %s agregado al universo (score=%.0f ret1m=%+.1f%%)",
                        ticker, score, ret_1m * 100)
        else:
            logger.debug("  ⊘ %s descartado por Claude/filtro", ticker)

    logger.info("Discovery: %d nuevos tickers para el analyst: %s", len(selected), selected)
    return selected
