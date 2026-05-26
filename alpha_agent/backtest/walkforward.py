"""
Backtester walk-forward.

Pseudocódigo:

    history = precios del universo
    equity = [capital_inicial]
    positions = {}
    for fecha t en rebalance_dates:
        window = history hasta t (últimos lookback días)
        capm = compute_capm_metrics(window, bench_window)
        tech = compute_technical_indicators(ohlc_hasta_t)
        scores = build_scores(capm, tech, closes=window)
        port = optimize_portfolio(window, capm['mu_anual'], ...)
        target_weights = top_n con guards + pesos Markowitz
        rebalance(positions, target_weights, precio=window.iloc[-1])
    trackear equity day-by-day con retornos reales

Supuestos conservadores:
    - Costo de transacción: 0.1% por trade (configurable).
    - Slippage: 0 (paper-conservative); upgrade futuro.
    - Dividendos: incluidos vía auto_adjust de yfinance.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from alpha_agent.analytics.capm import compute_capm_metrics
from alpha_agent.analytics.markowitz import optimize_portfolio
from alpha_agent.analytics.scoring import build_scores
from alpha_agent.analytics.technical import compute_technical_indicators
from alpha_agent.analytics.kelly import blend_markowitz_kelly
from alpha_agent.config import BENCHMARK_TICKER, PARAMS

logger = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    start_date: str
    end_date: str
    initial_capital: float
    final_equity: float
    total_return: float
    cagr: float
    sharpe: float
    max_drawdown: float
    n_rebalances: int
    avg_positions: float
    turnover_annual: float
    equity_curve: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    rebalance_log: list[dict] = field(default_factory=list)
    benchmark_cagr: float = 0.0
    benchmark_sharpe: float = 0.0
    benchmark_max_dd: float = 0.0
    sortino: float = 0.0
    calmar: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0

    def summary(self) -> str:
        alpha = self.cagr - self.benchmark_cagr
        pf_str = f"{self.profit_factor:.2f}" if self.profit_factor < 99 else ">99"
        lines = [
            "═" * 52,
            "   BACKTEST WALK-FORWARD — Alpha Agent",
            "═" * 52,
            f"  Período          {self.start_date} → {self.end_date}",
            f"  Capital inicial  ${self.initial_capital:,.2f}",
            f"  Capital final    ${self.final_equity:,.2f}",
            "",
            "  ── Estrategia ──────────────────────────────",
            f"  Retorno total    {self.total_return*100:+.2f}%",
            f"  CAGR             {self.cagr*100:+.2f}%",
            f"  Sharpe OOS       {self.sharpe:.2f}",
            f"  Sortino          {self.sortino:.2f}",
            f"  Calmar           {self.calmar:.2f}",
            f"  Max Drawdown     {self.max_drawdown*100:.2f}%",
            f"  Win Rate         {self.win_rate*100:.1f}%",
            f"  Profit Factor    {pf_str}",
            "",
        ]
        if self.benchmark_cagr:
            lines += [
                "  ── vs SPY Buy & Hold ───────────────────────",
                f"  CAGR SPY         {self.benchmark_cagr*100:+.2f}%",
                f"  Sharpe SPY       {self.benchmark_sharpe:.2f}",
                f"  Max DD SPY       {self.benchmark_max_dd*100:.2f}%",
                f"  Alpha generado   {alpha*100:+.2f}% anual",
                "",
            ]
        lines += [
            "  ── Operativa ───────────────────────────────",
            f"  Rebalanceos      {self.n_rebalances}",
            f"  Posiciones prom  {self.avg_positions:.1f}",
            f"  Turnover anual   {self.turnover_annual*100:.0f}%",
            "═" * 52,
        ]
        return "\n".join(lines)


def _max_drawdown(equity: pd.Series) -> float:
    roll_max = equity.cummax()
    dd = equity / roll_max - 1
    return float(dd.min())


def _annualized_sharpe(returns: pd.Series) -> float:
    if returns.std() == 0 or len(returns) < 2:
        return 0.0
    return float((returns.mean() - PARAMS.risk_free_rate / PARAMS.trading_days) / returns.std() * np.sqrt(PARAMS.trading_days))


def _annualized_sortino(returns: pd.Series) -> float:
    """Sortino ratio: penaliza solo la desviación negativa (no la volatilidad total)."""
    if len(returns) < 2:
        return 0.0
    rf_daily = PARAMS.risk_free_rate / PARAMS.trading_days
    excess = returns - rf_daily
    downside = excess[excess < 0]
    if len(downside) < 2 or downside.std() == 0:
        return 0.0
    return float(excess.mean() / downside.std() * np.sqrt(PARAMS.trading_days))


def _calmar(cagr: float, max_drawdown: float) -> float:
    """Calmar ratio = CAGR / |max drawdown|. Benchmark: >0.5 aceptable, >1.0 bueno."""
    return float(cagr / abs(max_drawdown)) if max_drawdown != 0 else 0.0


def _win_rate_and_profit_factor(rebalance_log: list[dict]) -> tuple[float, float]:
    """
    Win rate y profit factor por período de rebalanceo.
    Período = equity_after[i] → equity_before[i+1] (retorno real entre rebalanceos).
    """
    if len(rebalance_log) < 2:
        return 0.0, 0.0
    periods = []
    for i in range(len(rebalance_log) - 1):
        eq_start = rebalance_log[i]["equity_after"]
        eq_end   = rebalance_log[i + 1]["equity_before"]
        if eq_start > 0:
            periods.append((eq_end - eq_start) / eq_start)
    if not periods:
        return 0.0, 0.0
    wins   = [r for r in periods if r > 0]
    losses = [r for r in periods if r <= 0]
    win_rate     = len(wins) / len(periods)
    gross_profit = sum(wins)
    gross_loss   = abs(sum(losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 999.0
    return float(win_rate), float(profit_factor)


def _build_target_weights(
    closes_window: pd.DataFrame,
    benchmark_window: pd.Series,
    technical_snapshot: pd.DataFrame,
) -> pd.Series:
    """
    iter21: modela la estrategia LIVE real = sleeve CP momentum (el único activo).

    Antes usaba el sleeve LP (scores["long_term"]) × PARAMS.weight_long_term=0.0 →
    todo a cash → backtest 0%. Ahora usa scores["short_term"] (CP, ya filtrado a
    get_effective_cp_universe en build_scores), toma top_n_short_term, pondera por
    score dentro del sleeve y despliega weight_short_term (~0.90). Mismo scoring que
    el analyst live (CAPM + técnico + memoria), sin look-ahead (solo data hasta t).
    """
    capm = compute_capm_metrics(closes_window, benchmark_window)
    if capm.empty:
        return pd.Series(dtype=float)

    scores = build_scores(capm, technical_snapshot, closes=closes_window, backtest_mode=True)
    cp_df = scores.get("short_term")
    if cp_df is None or cp_df.empty:
        return pd.Series(dtype=float)

    n = max(1, int(PARAMS.top_n_short_term))
    top = cp_df.head(n)
    if top.empty:
        return pd.Series(dtype=float)

    # Peso por score dentro del sleeve (score_st puede ser negativo → shift a positivo).
    if "score_st" in top.columns:
        sc = top["score_st"].astype(float)
        sc = sc - sc.min() + 0.10   # asegura pesos positivos manteniendo el ranking
    else:
        sc = pd.Series(1.0, index=top.index)
    if sc.sum() <= 0:
        return pd.Series(dtype=float)
    w = sc / sc.sum()
    # iter35: aplicar el mismo cap + floor que el live (signals.py) para que el
    # backtest sea representativo. Sin esto, el backtest produce pesos sin tope y
    # el live operaba diferente → backtest no validaba el comportamiento real.
    from alpha_agent.reporting.signals import cap_floor_weights
    cap = float(getattr(PARAMS, "cp_max_weight_per_position", 0.35))
    capped = cap_floor_weights(w.tolist(), cap=cap, floor=0.12)
    w = pd.Series(capped, index=w.index)
    return w * float(PARAMS.weight_short_term)


def _technical_snapshot(ohlc: dict[str, pd.DataFrame], as_of: pd.Timestamp) -> pd.DataFrame:
    """
    Calcula indicadores técnicos reales (MACD, EMA, RSI, volumen, breakout)
    usando solo datos hasta `as_of` para evitar look-ahead bias.
    """
    # Recortar cada OHLC a la ventana permitida
    ohlc_window = {t: df.loc[df.index <= as_of] for t, df in ohlc.items() if len(df.loc[df.index <= as_of]) >= 60}
    if not ohlc_window:
        return pd.DataFrame()
    try:
        return compute_technical_indicators(ohlc_window)
    except Exception as e:
        logger.warning("Technical snapshot falló (%s), usando fallback simplificado.", e)
        rows = []
        for t, df in ohlc_window.items():
            close = df["Close"]
            last_price = float(close.iloc[-1])
            ret_1m = float(close.iloc[-1] / close.iloc[-21] - 1) if len(close) >= 22 else np.nan
            ret_3m = float(close.iloc[-1] / close.iloc[-63] - 1) if len(close) >= 64 else np.nan
            high_52w = float(close.tail(252).max())
            rows.append({
                "ticker": t, "price": last_price, "rsi": 50.0, "atr": 0.0,
                "stop_loss_atr": np.nan, "ret_1m": ret_1m, "ret_3m": ret_3m,
                "dist_52w_high": last_price / high_52w - 1 if high_52w > 0 else np.nan,
            })
        return pd.DataFrame(rows).set_index("ticker")


def run_backtest(
    closes: pd.DataFrame,
    ohlc: dict[str, pd.DataFrame],
    *,
    initial_capital: float = 500.0,
    lookback_days: int = 252,
    rebalance_every: int = 21,
    transaction_cost_bps: float = 10.0,   # 0.1%
) -> BacktestResult:
    """
    Ejecuta un backtest walk-forward sobre el universo.

    Args:
        closes: DataFrame ancho de cierres, incluido el BENCHMARK_TICKER.
        ohlc: dict ticker → OHLC (necesario para technical snapshot).
        initial_capital: equity al día 0.
        lookback_days: ventana usada para calcular CAPM/scores en cada rebalance.
        rebalance_every: frecuencia de rebalance en días hábiles (~21 = mensual).
        transaction_cost_bps: costo por trade en basis points del notional rotado.
    """
    if BENCHMARK_TICKER not in closes.columns:
        raise ValueError(f"Falta el benchmark {BENCHMARK_TICKER} en closes.")

    closes = closes.dropna(how="all").ffill().dropna(how="all")
    dates = closes.index
    if len(dates) <= lookback_days + 5:
        # Degradar automaticamente en lugar de crashear: adaptamos el lookback
        # al 60% de la historia disponible, con piso de 60 dias.
        adjusted = max(60, int(len(dates) * 0.6))
        if adjusted >= len(dates) - 5:
            raise ValueError(
                f"No hay suficiente historia para backtestear "
                f"(solo {len(dates)} filas; se necesitan al menos 66)."
            )
        import logging as _lg
        _lg.getLogger(__name__).warning(
            "Historia corta (%d filas). Reduciendo lookback %d → %d para walk-forward.",
            len(dates), lookback_days, adjusted,
        )
        lookback_days = adjusted

    # Fechas de rebalanceo
    rebalance_idx = list(range(lookback_days, len(dates), rebalance_every))
    rebalance_dates = [dates[i] for i in rebalance_idx]

    equity = initial_capital
    equity_curve: list[tuple[pd.Timestamp, float]] = []
    weights: pd.Series = pd.Series(dtype=float)
    rebalance_log: list[dict] = []
    total_turnover = 0.0

    for i, d in enumerate(dates):
        # aplicar retorno diario sobre pesos vigentes
        if i > 0 and not weights.empty:
            rets_today = (closes.loc[d] / closes.loc[dates[i - 1]] - 1).fillna(0)
            aligned = weights.reindex(rets_today.index).fillna(0)
            port_ret = float((aligned * rets_today).sum())
            equity *= 1 + port_ret

        # rebalanceo en fecha programada
        if d in rebalance_dates:
            window = closes.loc[:d].tail(lookback_days)
            bench = window[BENCHMARK_TICKER]
            tech_snap = _technical_snapshot(ohlc, d)
            new_weights = _build_target_weights(window, bench, tech_snap)

            if not new_weights.empty:
                # turnover y costos
                old = weights.reindex(new_weights.index.union(weights.index)).fillna(0)
                new = new_weights.reindex(old.index).fillna(0)
                turnover = float((new - old).abs().sum())
                cost = turnover * (transaction_cost_bps / 10000) * equity
                equity -= cost
                total_turnover += turnover

                rebalance_log.append({
                    "date": d.isoformat(),
                    "equity_before": round(equity + cost, 2),
                    "equity_after": round(equity, 2),
                    "turnover": round(turnover, 3),
                    "cost": round(cost, 2),
                    "weights": {k: round(float(v), 4) for k, v in new_weights.items()},
                })
                weights = new_weights

        equity_curve.append((d, equity))

    eq_series = pd.Series({d: v for d, v in equity_curve})
    eq_series.index = pd.DatetimeIndex(eq_series.index)
    daily_rets = eq_series.pct_change().dropna()

    total_return = eq_series.iloc[-1] / initial_capital - 1
    n_years = (eq_series.index[-1] - eq_series.index[0]).days / 365.25
    cagr = (1 + total_return) ** (1 / n_years) - 1 if n_years > 0 else 0
    sharpe  = _annualized_sharpe(daily_rets)
    sortino = _annualized_sortino(daily_rets)
    mdd     = _max_drawdown(eq_series)
    calmar  = _calmar(cagr, mdd)
    win_rate, profit_factor = _win_rate_and_profit_factor(rebalance_log)
    avg_positions = np.mean([len(r["weights"]) for r in rebalance_log]) if rebalance_log else 0
    n_years_eff = max(n_years, 0.01)
    turnover_ann = total_turnover / n_years_eff

    # ── Benchmark SPY buy & hold ──────────────────────────────────────
    bench_cagr = bench_sharpe = bench_dd = 0.0
    try:
        spy = closes[BENCHMARK_TICKER].reindex(eq_series.index).ffill().dropna()
        spy_eq = initial_capital * spy / spy.iloc[0]
        spy_rets = spy_eq.pct_change().dropna()
        spy_total = spy_eq.iloc[-1] / initial_capital - 1
        bench_cagr  = (1 + spy_total) ** (1 / n_years_eff) - 1
        bench_sharpe = _annualized_sharpe(spy_rets)
        bench_dd    = _max_drawdown(spy_eq)
    except Exception as e:
        logger.debug("Benchmark calc falló: %s", e)

    return BacktestResult(
        start_date=str(eq_series.index[0].date()),
        end_date=str(eq_series.index[-1].date()),
        initial_capital=initial_capital,
        final_equity=float(eq_series.iloc[-1]),
        total_return=float(total_return),
        cagr=float(cagr),
        sharpe=float(sharpe),
        max_drawdown=float(mdd),
        n_rebalances=len(rebalance_log),
        avg_positions=float(avg_positions),
        turnover_annual=float(turnover_ann),
        equity_curve=eq_series,
        rebalance_log=rebalance_log,
        benchmark_cagr=float(bench_cagr),
        benchmark_sharpe=float(bench_sharpe),
        benchmark_max_dd=float(bench_dd),
        sortino=float(sortino),
        calmar=float(calmar),
        win_rate=float(win_rate),
        profit_factor=float(profit_factor),
    )
