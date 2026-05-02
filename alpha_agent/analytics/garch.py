"""
GARCH(1,1) volatility forecasting + CVaR for position sizing.

forecast_garch_vol(): annualized 1-step σ forecast with arch fallback to realized vol.
compute_cvar():       Expected Shortfall at given confidence level.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def forecast_garch_vol(returns: pd.Series, horizon: int = 1) -> float:
    """
    Annualized GARCH(1,1) volatility forecast for next `horizon` periods.
    Falls back to realized historical vol if arch is not installed or fit fails.

    Args:
        returns: Daily log-returns series (not %-scaled).
        horizon: Forecast horizon in trading days.

    Returns:
        Annualized volatility as a decimal (e.g., 0.28 = 28%).
    """
    clean = returns.dropna()
    if len(clean) < 60:
        return float(clean.std() * np.sqrt(252))

    try:
        from arch import arch_model  # type: ignore
        model = arch_model(clean * 100, vol="Garch", p=1, q=1, dist="Normal")
        fit   = model.fit(disp="off", show_warning=False)
        fc    = fit.forecast(horizon=horizon, reindex=False)
        sigma_daily = float(np.sqrt(fc.variance.iloc[-1, 0])) / 100
        vol_annual  = sigma_daily * np.sqrt(252)
        logger.debug("GARCH(1,1) σ forecast: %.1f%% (horizon=%d)", vol_annual * 100, horizon)
        return vol_annual
    except Exception as e:
        logger.debug("GARCH fallback to realized vol: %s", e)
        return float(clean.std() * np.sqrt(252))


def compute_cvar(returns: pd.Series, confidence: float = 0.95) -> float:
    """
    Conditional Value at Risk (Expected Shortfall) at `confidence` level.
    Returns the average loss in the worst (1-confidence)% of cases, as a
    positive decimal (e.g., 0.032 = 3.2% expected daily loss in tail).

    Args:
        returns: Daily log-returns series.
        confidence: Confidence level (0.95 = 95% CVaR).

    Returns:
        CVaR as positive decimal.
    """
    clean = returns.dropna()
    if len(clean) < 20:
        return float(abs(clean.mean()) + 2 * clean.std())

    threshold = float(clean.quantile(1 - confidence))
    tail      = clean[clean <= threshold]
    if tail.empty:
        return float(abs(threshold))
    return float(abs(tail.mean()))
