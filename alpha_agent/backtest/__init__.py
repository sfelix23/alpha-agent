"""
Backtester walk-forward del alpha_agent.

Objetivo: validar out-of-sample si la estrategia tiene edge real,
NO overfitting del Sharpe in-sample.

Metodología:
    - Lookback window (ej. 252 días) para calcular CAPM + scoring + Markowitz.
    - Rebalanceo cada N días (ej. 21 = mensual).
    - En cada fecha de rebalanceo, el modelo solo "ve" datos HASTA esa fecha
      y decide la cartera para el siguiente período.
    - Se trackea el equity curve con costos de transacción y se reportan:
      CAGR, Sharpe out-of-sample, max drawdown, win rate, turnover.

Esto NO es un backtest perfecto (sigue habiendo bias de selección del universo,
survival bias en los tickers vivos, etc.) pero es infinitamente mejor que
ningún backtest.
"""
from .walkforward import run_backtest, BacktestResult

__all__ = ["run_backtest", "BacktestResult"]
