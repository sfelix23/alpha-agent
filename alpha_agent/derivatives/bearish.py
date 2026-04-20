"""
Bearish scoring — rankea activos como candidatos para apostar a la baja.

La lógica es el espejo del long scoring pero con condiciones de setup bajista:

    score_bear = 0.35 * z(-alpha_jensen)      # alpha negativo vs mercado
                + 0.25 * z(rsi > 70)          # sobrecompra (riesgo de reversión)
                + 0.20 * z(-ret_1m)           # momentum negativo
                + 0.10 * z(beta_if_bear)      # beta alta castigada en bear
                + 0.10 * z(-sentiment)        # sentiment negativo de noticias

Solo aplica si se cumplen pre-condiciones de entorno:
    - Régimen macro bear o sideways, O
    - VIX > 20

En régimen bull puro las apuestas bajistas idiosincráticas son posibles pero
tienen tail risk muy alto (squeeze), así que se descartan salvo casos extremos.

Devuelve un DataFrame ordenado por score_bear descendente, listo para que
`options_builder.build_directional_options_signals` tome los top_n_bearish y
arme contratos de puts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from alpha_agent.config import PARAMS, SECTOR_MAP
from alpha_agent.macro.macro_context import MacroSnapshot

logger = logging.getLogger(__name__)


def _zscore(series: pd.Series) -> pd.Series:
    s = series.astype(float).copy()
    if s.std(ddof=0) == 0 or s.dropna().empty:
        return pd.Series(0.0, index=series.index)
    return (s - s.mean()) / s.std(ddof=0)


def _bearish_environment(macro: MacroSnapshot) -> tuple[bool, str]:
    """
    Decide si el entorno macro habilita apuestas bajistas idiosincráticas.
    Retorna (allowed, reason).
    """
    vix = macro.prices.get("vix", 15.0) or 15.0
    if macro.regime == "bear":
        return True, f"régimen bear ({macro.regime_reason})"
    if macro.regime == "sideways":
        return True, f"régimen lateral (mean-reversion de sobrecompras)"
    if vix > 22:
        return True, f"VIX elevado ({vix:.0f}) habilita cobertura táctica"
    return False, f"régimen bull + VIX bajo ({vix:.0f}) → apuestas bajistas muy arriesgadas"


def build_bearish_candidates(
    capm: pd.DataFrame,
    technical: pd.DataFrame,
    sentiments: dict[str, float] | None,
    macro: MacroSnapshot,
) -> pd.DataFrame:
    """
    Devuelve un DataFrame ordenado por score_bear descendente.

    Args:
        capm: DataFrame CAPM (beta, alpha_jensen, sharpe, sigma_anual, ...).
        technical: DataFrame técnico (rsi, ret_1m, ret_3m, dist_52w_high, ...).
        sentiments: dict ticker → sentiment.score (puede ser None si no se calculó).
        macro: MacroSnapshot global.

    Returns:
        DataFrame con score_bear, vacío si el entorno no habilita bajistas.
    """
    allowed, reason = _bearish_environment(macro)
    logger.info("Bearish environment: %s (%s)", "ON" if allowed else "OFF", reason)
    if not allowed:
        return pd.DataFrame()

    df = capm.join(technical, how="inner").copy()
    # Excluir ETFs e índices del bucket bearish direccional
    df = df[~df.index.isin({"SPY", "QQQ", "GLD", "IBIT", "ETHE"})]

    # Sentiment lookup
    if sentiments is None:
        sentiments = {}
    df["sentiment"] = df.index.map(lambda t: sentiments.get(t, 0.0))

    # Precondiciones mínimas de setup bajista — si no las cumple ni entra
    pre = (
        (df["alpha_jensen"].fillna(0) < 0)       # alpha negativo
        & (df["ret_1m"].fillna(0) < 0.02)        # no momentum alcista fuerte
    )
    df = df[pre]
    if df.empty:
        logger.info("Ningún activo cumple precondiciones bearish (alpha<0 & ret_1m<2%%).")
        return pd.DataFrame()

    rsi = df["rsi"].fillna(50)
    overbought_signal = (rsi - PARAMS.rsi_overbought).clip(lower=0)

    beta = df["beta"].fillna(1.0)
    beta_bear_penalty = beta.clip(lower=1.0) - 1.0   # beta > 1 castigado más en bear

    score_bear = (
        0.35 * _zscore(-df["alpha_jensen"].fillna(0))
        + 0.25 * _zscore(overbought_signal)
        + 0.20 * _zscore(-df["ret_1m"].fillna(0))
        + 0.10 * _zscore(beta_bear_penalty)
        + 0.10 * _zscore(-df["sentiment"])
    )
    df["score_bear"] = score_bear
    df["sector"] = df.index.map(lambda t: SECTOR_MAP.get(t, "Other"))
    df = df.sort_values("score_bear", ascending=False)

    # Guard: máximo 2 por sector también en el bucket bearish
    sector_count: dict[str, int] = {}
    keep = []
    for t, row in df.iterrows():
        s = row["sector"]
        if sector_count.get(s, 0) >= 2:
            continue
        keep.append(t)
        sector_count[s] = sector_count.get(s, 0) + 1
    df = df.loc[keep]

    logger.info("Top bearish candidates: %s", df.head(PARAMS.top_n_bearish).index.tolist())
    return df
