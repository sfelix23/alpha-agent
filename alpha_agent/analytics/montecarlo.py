"""
Monte Carlo simulation — cuantifica el riesgo real del portfolio.

Simula 10,000 paths del portfolio basados en retornos históricos
para obtener: DD máximo esperado al 95%, VaR, distribución de CAGR.

Usado en: run_dashboard.py (tab Resumen), run_backtest.py
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class MonteCarloResult:
    n_simulations: int
    horizon_days: int
    # Distribución de retorno final
    median_return_pct: float
    p10_return_pct: float      # percentil 10 (pessimista)
    p90_return_pct: float      # percentil 90 (optimista)
    # Drawdown máximo
    expected_max_dd_pct: float     # mediana de DD máximo
    worst_case_max_dd_pct: float   # percentil 95 de DD máximo
    # VaR y CVaR diario
    var_95_daily_pct: float    # Value at Risk 95% — pérdida máxima en 1 día (95% conf)
    cvar_95_daily_pct: float   # Expected Shortfall — pérdida promedio en el peor 5%
    # Probabilidades
    prob_positive_pct: float   # probabilidad de retorno positivo
    prob_beat_spy_pct: float   # prob de superar SPY (asumiendo 10% anual SPY)
    # Capital
    initial_capital: float
    median_final_capital: float
    worst_case_capital: float  # percentil 5


def run_simulation(
    daily_returns: np.ndarray,
    initial_capital: float = 1600.0,
    horizon_days: int = 252,
    n_simulations: int = 10_000,
    spy_annual_return: float = 0.10,
) -> MonteCarloResult:
    """
    Simula n_simulations paths de `horizon_days` días.

    Args:
        daily_returns: array de retornos diarios históricos (decimales, ej: 0.012)
        initial_capital: capital inicial en USD
        horizon_days: horizonte de simulación (252 = 1 año)
        n_simulations: número de paths
        spy_annual_return: retorno diario esperado de SPY para comparar

    Returns:
        MonteCarloResult con métricas de riesgo y retorno.
    """
    if len(daily_returns) < 20:
        logger.warning("Insuficientes retornos históricos para Monte Carlo (%d)", len(daily_returns))
        return _empty_result(initial_capital, n_simulations, horizon_days)

    mu    = float(np.mean(daily_returns))
    sigma = float(np.std(daily_returns, ddof=1))

    rng = np.random.default_rng(seed=42)
    # Shape: (n_simulations, horizon_days)
    sim_returns = rng.normal(loc=mu, scale=sigma, size=(n_simulations, horizon_days))

    # Portfolio paths: valor acumulado
    # paths[i, t] = valor al día t en la simulación i
    cum_returns  = np.cumprod(1 + sim_returns, axis=1)
    paths        = initial_capital * cum_returns          # (n_sim, horizon_days)
    final_values = paths[:, -1]

    # Retornos finales en %
    final_returns_pct = (final_values / initial_capital - 1) * 100

    # Drawdown máximo por path
    running_max = np.maximum.accumulate(paths, axis=1)
    drawdowns   = (paths - running_max) / running_max     # negativo
    max_dd_per_path = np.min(drawdowns, axis=1) * 100     # % negativo

    # VaR y CVaR diarios (sobre distribución de retornos simulados del día 1)
    all_day1 = sim_returns[:, 0] * 100   # % retorno día 1
    var_95   = float(np.percentile(all_day1, 5))   # percentil 5 (pérdida)
    cvar_95  = float(np.mean(all_day1[all_day1 <= var_95]))

    # SPY benchmark diario
    spy_daily = (1 + spy_annual_return) ** (1 / 252) - 1
    spy_final = initial_capital * (1 + spy_daily) ** horizon_days
    prob_beat_spy = float(np.mean(final_values > spy_final) * 100)

    return MonteCarloResult(
        n_simulations=n_simulations,
        horizon_days=horizon_days,
        median_return_pct=float(np.median(final_returns_pct)),
        p10_return_pct=float(np.percentile(final_returns_pct, 10)),
        p90_return_pct=float(np.percentile(final_returns_pct, 90)),
        expected_max_dd_pct=float(np.median(max_dd_per_path)),
        worst_case_max_dd_pct=float(np.percentile(max_dd_per_path, 95)),
        var_95_daily_pct=var_95,
        cvar_95_daily_pct=cvar_95,
        prob_positive_pct=float(np.mean(final_returns_pct > 0) * 100),
        prob_beat_spy_pct=prob_beat_spy,
        initial_capital=initial_capital,
        median_final_capital=float(np.median(final_values)),
        worst_case_capital=float(np.percentile(final_values, 5)),
    )


def _empty_result(initial_capital: float, n_sim: int, horizon: int) -> MonteCarloResult:
    return MonteCarloResult(
        n_simulations=n_sim, horizon_days=horizon,
        median_return_pct=0, p10_return_pct=0, p90_return_pct=0,
        expected_max_dd_pct=0, worst_case_max_dd_pct=0,
        var_95_daily_pct=0, cvar_95_daily_pct=0,
        prob_positive_pct=50, prob_beat_spy_pct=50,
        initial_capital=initial_capital,
        median_final_capital=initial_capital,
        worst_case_capital=initial_capital,
    )


def run_from_portfolio_history(
    history: list[dict],
    initial_capital: float = 1600.0,
) -> MonteCarloResult | None:
    """
    Wrapper que acepta el formato de historial de Alpaca
    (lista de {"ts": int, "equity": float}) y corre la simulación.
    """
    if len(history) < 5:
        return None
    try:
        equities = np.array([h["equity"] for h in history], dtype=float)
        daily_returns = np.diff(equities) / equities[:-1]
        daily_returns = daily_returns[np.isfinite(daily_returns)]
        if len(daily_returns) < 5:
            return None
        return run_simulation(daily_returns, initial_capital=initial_capital)
    except Exception as e:
        logger.warning("Monte Carlo failed: %s", e)
        return None
