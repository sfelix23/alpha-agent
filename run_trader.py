"""
Entry point del Agente 2 (ejecutor).

Por default:
    - usa AlpacaBroker en paper trading
    - corre en dry_run (no envía órdenes reales) — explícitamente hay que pasar --live

Ejemplos:
    python run_trader.py                  # dry run, paper, no envía órdenes
    python run_trader.py --live           # paper trading REAL (se mandan a Alpaca paper)
    python run_trader.py --live --notify  # además avisa por WhatsApp
    python run_trader.py --capital 5000   # tope manual de capital
"""

from __future__ import annotations

import argparse
import logging
import sys

# Fix para Windows cp1252: forzar stdout/stderr a UTF-8
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.platform == "win32" and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv

from trader_agent.brokers import AlpacaBroker
from trader_agent.strategy import execute, notify, summarize_fills


def main() -> None:
    parser = argparse.ArgumentParser(description="Trader Agent — ejecutor de señales del Alpha Agent.")
    parser.add_argument("--live", action="store_true", help="Enviar órdenes reales (paper trading).")
    parser.add_argument("--notify", action="store_true", help="Avisar fills por WhatsApp.")
    parser.add_argument("--capital", type=float, default=None, help="Tope de capital a usar (USD).")
    args = parser.parse_args()

    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("trader_agent")

    broker = AlpacaBroker(paper=True)  # 🚨 fuerzo paper, no tocar sin volver acá
    fills = execute(broker, dry_run=not args.live, max_capital=args.capital)

    print("\n" + "═" * 70)
    print(summarize_fills(fills))
    print("═" * 70 + "\n")

    if args.notify and fills:
        notify(fills)


if __name__ == "__main__":
    main()
