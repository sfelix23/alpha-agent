"""
CAPM: Beta, alfa de Jensen, retorno esperado teórico, Sharpe ratio.

Para cada activo i:
    r_i,t  = log-returns diarios
    β_i    = cov(r_i, r_m) / var(r_m)
    α_i    = E[r_i] − [r_f + β_i · (E[r_m] − r_f)]    (Jensen)
    E[r_i] = retorno medio histórico anualizado
    σ_i    = volatilidad anualizada
    Sharpe = (E[r_i] − r_f) / σ_i

Todo se anualiza con 252 días.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from alpha_agent.config import PARAMS


def _log_returns(prices: pd.DataFrame | pd.Series) -> pd.DataFrame | pd.Series:
    return np.log(prices / prices.shift(1)).dropna(how="all")


def _ewma_mean(series: pd.Series, halflife: int = 63) -> float:
    """Media exponencialmente ponderada — más peso a datos recientes (63d ≈ 1 trimestre)."""
    weights = np.array([(0.5 ** (1 / halflife)) ** i for i in range(len(series) - 1, -1, -1)])
    weights /= weights.sum()
    return float((series.values * weights).sum())


def compute_capm_metrics(closes: pd.DataFrame, benchmark: pd.Series) -> pd.DataFrame:
    """
    Calcula métricas CAPM por activo con EWMA para mayor relevancia de datos recientes.

    Args:
        closes: DataFrame ancho de cierres (index=fecha, cols=ticker).
        benchmark: serie de cierres del benchmark (ej. SPY).

    Returns:
        DataFrame indexado por ticker con columnas:
            mu_anual, sigma_anual, beta, alpha_jensen,
            expected_return_capm, sharpe, information_ratio
    """
    rets = _log_returns(closes)
    bench_rets = _log_returns(benchmark).dropna()

    # alinear fechas
    common = rets.index.intersection(bench_rets.index)
    rets = rets.loc[common]
    bench_rets = bench_rets.loc[common]

    rf = PARAMS.risk_free_rate
    td = PARAMS.trading_days

    mkt_var = bench_rets.var()
    # EWMA para retorno esperado del mercado — más peso a últimos 3 meses
    mkt_mu_anual = _ewma_mean(bench_rets) * td

    rows = []
    for ticker in rets.columns:
        r = rets[ticker].dropna()
        if len(r) < PARAMS.min_obs:
            continue
        r_aligned, m_aligned = r.align(bench_rets, join="inner")
        if len(r_aligned) < PARAMS.min_obs:
            continue

        cov = np.cov(r_aligned.values, m_aligned.values, ddof=1)[0, 1]
        beta = cov / mkt_var

        # EWMA mu: más peso a retornos recientes (63d halflife)
        mu = _ewma_mean(r_aligned) * td
        sigma = r_aligned.std(ddof=1) * np.sqrt(td)

        expected_capm = rf + beta * (mkt_mu_anual - rf)
        alpha_jensen = mu - expected_capm
        sharpe = (mu - rf) / sigma if sigma > 0 else np.nan

        # Information Ratio: consistencia del alpha vs tracking error
        excess_daily = r_aligned.values - m_aligned.values
        tracking_error = float(np.std(excess_daily, ddof=1)) * np.sqrt(td)
        ir = alpha_jensen / tracking_error if tracking_error > 0 else np.nan

        rows.append({
            "ticker": ticker,
            "mu_anual": mu,
            "sigma_anual": sigma,
            "beta": beta,
            "alpha_jensen": alpha_jensen,
            "expected_return_capm": expected_capm,
            "sharpe": sharpe,
            "information_ratio": ir,
        })

    return pd.DataFrame(rows).set_index("ticker")
