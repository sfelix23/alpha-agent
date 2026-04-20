"""
Entry point del backtester walk-forward.

Uso:
    python run_backtest.py                     # backtest default (1y lookback, rebalance mensual)
    python run_backtest.py --capital 1000      # capital inicial
    python run_backtest.py --lookback 126      # lookback de 6m
    python run_backtest.py --rebalance 10      # rebalance cada 10 días
    python run_backtest.py --save              # guarda equity curve a CSV

El backtest usa el mismo scoring/Markowitz que el live, pero solo con data
hasta la fecha t en cada rebalanceo (walk-forward, no look-ahead).
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime

from dotenv import load_dotenv

from alpha_agent.backtest import run_backtest
from alpha_agent.config import PARAMS, PATHS
from alpha_agent.data import download_universe


def main() -> None:
    parser = argparse.ArgumentParser(description="Walk-forward backtest del alpha_agent.")
    parser.add_argument("--capital", type=float, default=PARAMS.paper_capital_usd)
    parser.add_argument("--lookback", type=int, default=252, help="Ventana en días hábiles.")
    parser.add_argument("--rebalance", type=int, default=21, help="Frecuencia rebalance en días hábiles.")
    parser.add_argument("--cost-bps", type=float, default=10.0, help="Costo transacción en bps.")
    parser.add_argument("--save", action="store_true", help="Guardar equity curve y log a disco.")
    args = parser.parse_args()

    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("backtest")

    log.info("📉 Descargando universo histórico…")
    closes, ohlc = download_universe()

    log.info("🔁 Corriendo walk-forward: lookback=%d, rebalance=%d, costo=%.0fbps",
             args.lookback, args.rebalance, args.cost_bps)
    result = run_backtest(
        closes, ohlc,
        initial_capital=args.capital,
        lookback_days=args.lookback,
        rebalance_every=args.rebalance,
        transaction_cost_bps=args.cost_bps,
    )

    print("\n" + result.summary() + "\n")

    if args.save:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = PATHS.root / "backtests"
        out_dir.mkdir(exist_ok=True)
        result.equity_curve.to_csv(out_dir / f"equity_{ts}.csv", header=["equity"])
        (out_dir / f"rebalances_{ts}.json").write_text(
            json.dumps(result.rebalance_log, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        log.info("Resultados guardados en %s", out_dir)


if __name__ == "__main__":
    main()
