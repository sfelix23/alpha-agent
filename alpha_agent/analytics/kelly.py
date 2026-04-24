"""
Kelly Criterion para position sizing óptimo.

Fórmula continua (Gaussian):
    f* = (μ - rf) / σ²

Usamos half-Kelly (f*/2) para reducir varianza. El resultado se normaliza
dentro del sleeve y se capea en PARAMS.max_weight_per_asset.

blend_markowitz_kelly() combina Markowitz (covarianza) con Kelly (edge real)
en proporción configurable (default 60/40).
"""

from __future__ import annotations

import logging

import pandas as pd

from alpha_agent.config import PARAMS

logger = logging.getLogger(__name__)

_HALF_KELLY = 0.5
_MIN_SIGMA  = 0.01


def kelly_weights(capm: pd.DataFrame) -> pd.Series:
    """
    Half-Kelly normalizado para cada activo del DataFrame CAPM.
    Activos con edge negativo reciben peso 0.
    """
    rf    = PARAMS.risk_free_rate
    w_max = PARAMS.max_weight_per_asset

    fractions: dict[str, float] = {}
    for ticker in capm.index:
        mu    = float(capm.loc[ticker, "mu_anual"])
        sigma = float(capm.loc[ticker, "sigma_anual"])
        if sigma < _MIN_SIGMA:
            fractions[ticker] = 0.0
            continue
        f_star = (mu - rf) / (sigma ** 2)
        fractions[ticker] = max(0.0, f_star * _HALF_KELLY)

    series = pd.Series(fractions)
    total  = series.sum()
    if total <= 0:
        n = max(len(series), 1)
        return pd.Series(1.0 / n, index=series.index)

    series = series / total

    # Cap iterativo: exceso se redistribuye entre los no capados
    for _ in range(10):
        over = series > w_max
        if not over.any():
            break
        excess = (series[over] - w_max).sum()
        series[over] = w_max
        under = ~over & (series > 0)
        if under.any():
            series[under] += excess * (series[under] / series[under].sum())

    s = series.sum()
    return series / s if s > 0 else series


def blend_markowitz_kelly(
    markowitz_weights: pd.Series,
    capm: pd.DataFrame,
    *,
    kelly_alpha: float = 0.30,
) -> pd.Series:
    """
    final = (1 - kelly_alpha) * markowitz + kelly_alpha * kelly

    Args:
        kelly_alpha: peso de Kelly en el blend (default 30% — quarter-Kelly probado
                     óptimo por investigación: reduce drawdowns 20-30% manteniendo el edge).

    Returns:
        Serie renormalizada a suma = 1.
    """
    tickers  = markowitz_weights[markowitz_weights > 0].index.tolist()
    capm_sub = capm.loc[[t for t in tickers if t in capm.index]]
    if capm_sub.empty:
        return markowitz_weights

    k_w = kelly_weights(capm_sub)
    mw  = markowitz_weights.reindex(k_w.index).fillna(0.0)
    if mw.sum() > 0:
        mw = mw / mw.sum()

    blended = (1 - kelly_alpha) * mw + kelly_alpha * k_w
    s = blended.sum()
    if s > 0:
        blended = blended / s

    logger.info(
        "Kelly blend α=%.0f%% → %s",
        kelly_alpha * 100,
        {t: f"{w:.1%}" for t, w in blended[blended > 0].items()},
    )
    return blended
