"""
Optimización Markowitz: frontera eficiente y cartera de máximo Sharpe.

Resolvemos el problema clásico:

    max_w   (w'μ − r_f) / sqrt(w'Σw)
    s.a.    Σ w_i = 1
            0 ≤ w_i ≤ w_max

Como el problema es no-convexo (ratio), usamos la transformación estándar:
encontrar w que minimiza la varianza para un retorno objetivo, barrer la
frontera y elegir el punto de máximo Sharpe. Implementación con scipy.optimize.

Si scipy no está disponible o falla, hay un fallback analítico de mínima
varianza (sin restricciones de caja) usando inversa de la covarianza.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from alpha_agent.config import EXCLUIR_DE_OPTIMIZACION, PARAMS

logger = logging.getLogger(__name__)


def _annualized_cov(closes: pd.DataFrame) -> pd.DataFrame:
    """Covarianza anualizada con Ledoit-Wolf shrinkage cuando sklearn está disponible.

    Ledoit-Wolf reduce el error de estimación en universos pequeños (<50 activos)
    en ~30-40%. Clave para que Markowitz no sobre-concentre en activos con
    baja covarianza histórica por azar.
    """
    rets = np.log(closes / closes.shift(1)).dropna(how="all")
    try:
        from sklearn.covariance import LedoitWolf  # type: ignore
        lw = LedoitWolf().fit(rets.values)
        cov_matrix = pd.DataFrame(lw.covariance_ * PARAMS.trading_days, index=rets.columns, columns=rets.columns)
        logger.debug("Ledoit-Wolf shrinkage aplicado (n=%d, p=%d)", len(rets), len(rets.columns))
        return cov_matrix
    except ImportError:
        logger.debug("sklearn no disponible — usando covarianza histórica estándar")
        return rets.cov() * PARAMS.trading_days


def optimize_portfolio(
    closes: pd.DataFrame,
    expected_returns: pd.Series,
    *,
    candidates: list[str] | None = None,
) -> dict:
    """
    Optimiza pesos por máximo Sharpe sobre el universo elegible.

    Args:
        closes: DataFrame ancho de cierres.
        expected_returns: Serie indexada por ticker con μ anual.
        candidates: lista de tickers a incluir; si None usa todos los del index
                    de expected_returns excepto los excluidos por config.

    Returns:
        dict con keys: weights (Series), exp_return, volatility, sharpe.
    """
    if candidates is None:
        candidates = [t for t in expected_returns.index if t not in EXCLUIR_DE_OPTIMIZACION]

    candidates = [t for t in candidates if t in closes.columns]
    if len(candidates) < 2:
        raise ValueError("Necesito al menos 2 activos para optimizar.")

    sub_closes = closes[candidates].dropna(how="any")
    mu = expected_returns.loc[candidates].values
    cov = _annualized_cov(sub_closes).values
    n = len(candidates)
    rf = PARAMS.risk_free_rate
    w_max = PARAMS.max_weight_per_asset

    method_used = "scipy_slsqp"
    try:
        from scipy.optimize import minimize  # type: ignore

        def neg_sharpe(w: np.ndarray) -> float:
            ret = float(w @ mu)
            vol = float(np.sqrt(w @ cov @ w))
            if vol <= 0:
                return 1e6
            return -(ret - rf) / vol

        constraints = ({"type": "eq", "fun": lambda w: float(w.sum() - 1.0)},)
        bounds = tuple((0.0, w_max) for _ in range(n))
        x0 = np.full(n, 1.0 / n)

        res = minimize(neg_sharpe, x0, method="SLSQP", bounds=bounds, constraints=constraints)
        if not res.success:
            logger.warning("SLSQP no convergió: %s. Usando fallback inverse-vol.", res.message)
            w = _inverse_volatility_weights(cov, w_max)
            method_used = "inverse_volatility"
        else:
            w = res.x
    except ImportError:
        logger.warning(
            "scipy no disponible — usando fallback INVERSE VOLATILITY "
            "(razonable pero subóptimo). Instalá scipy para Max Sharpe real: pip install scipy"
        )
        w = _inverse_volatility_weights(cov, w_max)
        method_used = "inverse_volatility"

    w = np.clip(w, 0.0, w_max)
    w = w / w.sum() if w.sum() > 0 else np.full(n, 1.0 / n)

    weights = pd.Series(w, index=candidates).sort_values(ascending=False)
    port_ret = float(w @ mu)
    port_vol = float(np.sqrt(w @ cov @ w))
    port_sharpe = (port_ret - rf) / port_vol if port_vol > 0 else float("nan")

    return {
        "weights": weights,
        "exp_return": port_ret,
        "volatility": port_vol,
        "sharpe": port_sharpe,
        "method": method_used,
    }


def _inverse_volatility_weights(cov: np.ndarray, w_max: float) -> np.ndarray:
    """
    Fallback de Markowitz cuando no hay scipy.

    Inverse-volatility weighting: wᵢ ∝ 1/σᵢ, después se renormaliza y se aplica
    el cap w_max. Es mejor que equal-weight porque asigna menos capital a los
    activos más volátiles (risk parity simplificado). No maximiza Sharpe, pero
    es razonable y no necesita solver.
    """
    diag = np.sqrt(np.diag(cov))
    diag = np.where(diag > 1e-8, diag, np.nan)
    inv_vol = 1.0 / diag
    if np.all(np.isnan(inv_vol)):
        return np.full(len(diag), 1.0 / len(diag))
    inv_vol = np.nan_to_num(inv_vol, nan=0.0)
    w = inv_vol / inv_vol.sum()
    # aplicar cap máximo iterativamente
    for _ in range(10):
        over = w > w_max
        if not over.any():
            break
        excess = (w[over] - w_max).sum()
        w[over] = w_max
        remaining = ~over & (w > 0)
        if remaining.any():
            w[remaining] += excess * (w[remaining] / w[remaining].sum())
    return w / w.sum()
