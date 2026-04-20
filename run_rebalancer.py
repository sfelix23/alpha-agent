"""
Agente 4 — Rebalancer semanal de cartera.

Corre los viernes a las 15:00 ART (vía Task Scheduler).
Compara pesos actuales en Alpaca contra targets del último
signals/latest.json. Si un activo se desvió más del umbral, ajusta.

Uso:
    python run_rebalancer.py              # dry-run
    python run_rebalancer.py --live       # ejecuta en Alpaca paper
    python run_rebalancer.py --threshold 0.05
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR     = Path(__file__).parent.resolve()
SIGNALS_PATH = BASE_DIR / "signals" / "latest.json"
LOG_DIR      = BASE_DIR / "logs"

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.platform == "win32" and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logger = logging.getLogger("rebalancer")


def setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / f"rebalancer_{datetime.now().strftime('%Y-%m-%d')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(str(log_file), encoding="utf-8"),
        ],
    )


def load_targets(path: Path) -> dict[str, dict]:
    """Devuelve {ticker: portfolio_weight} desde latest.json."""
    if not path.exists():
        return {}
    try:
        data   = json.loads(path.read_text(encoding="utf-8"))
        params = data.get("params", {})
    except Exception as e:
        logger.warning("No se pudo leer signals: %s", e)
        return {}

    sleeve_map = {
        "long_term":  params.get("weight_long_term",  0.55),
        "short_term": params.get("weight_short_term", 0.25),
    }
    targets: dict[str, dict] = {}
    for bucket, sleeve_w in sleeve_map.items():
        for sig in data.get(bucket, []):
            ticker = sig.get("ticker")
            wt     = sig.get("weight_target", 0)
            if ticker and wt > 0:
                targets[ticker] = {
                    "portfolio_weight": wt * sleeve_w,
                    "horizon": bucket,
                }
    return targets


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live",      action="store_true")
    parser.add_argument("--threshold", type=float, default=0.08)
    args = parser.parse_args()

    load_dotenv(BASE_DIR / ".env")
    setup_logging()
    logger.info("=== REBALANCER START === live=%s threshold=%.1f%%",
                args.live, args.threshold * 100)

    from trader_agent.brokers.alpaca_broker import AlpacaBroker
    from alpha_agent.notifications import send_whatsapp

    broker = AlpacaBroker(paper=True)

    try:
        equity    = broker.get_equity()
        positions = broker.get_positions()
    except Exception as e:
        logger.error("Error conectando a Alpaca: %s", e)
        return

    if not positions:
        logger.info("Sin posiciones abiertas.")
        return

    targets  = load_targets(SIGNALS_PATH)
    if not targets:
        logger.warning("Sin targets en signals/latest.json")
        return

    sells:        list[dict] = []
    buys:         list[dict] = []
    report_lines: list[str]  = []

    equity_positions = {p.ticker: p for p in positions
                        if getattr(p, "asset_class", "equity") == "equity"}

    for ticker, pos in equity_positions.items():
        current_w   = pos.market_value / equity if equity > 0 else 0
        target_info = targets.get(ticker)
        if not target_info:
            report_lines.append(f"  ⚪ {ticker}: sin señal activa")
            continue

        target_w = target_info["portfolio_weight"]
        drift    = current_w - target_w

        if abs(drift) < args.threshold:
            report_lines.append(
                f"  ✅ {ticker}: {current_w:.1%} ≈ {target_w:.1%} (Δ{drift:+.1%})"
            )
            continue

        notional = abs(drift) * equity
        entry    = {"ticker": ticker, "notional": notional,
                    "drift": drift, "current_w": current_w, "target_w": target_w}
        if drift > 0:
            sells.append(entry)
            report_lines.append(
                f"  🔴 SELL {ticker}: ${notional:.0f} ({current_w:.1%}→{target_w:.1%})"
            )
        else:
            buys.append(entry)
            report_lines.append(
                f"  🟢 BUY  {ticker}: ${notional:.0f} ({current_w:.1%}→{target_w:.1%})"
            )

    executed: list[str] = []
    errors:   list[str] = []

    if args.live and (sells or buys):
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        for entry in sells + buys:           # SELLs primero → libera cash
            ticker   = entry["ticker"]
            side     = OrderSide.SELL if entry["drift"] > 0 else OrderSide.BUY
            try:
                price = broker.get_last_price(ticker)
                qty   = round(entry["notional"] / price, 4) if price > 0 else 0
                if qty <= 0:
                    continue
                broker._trading.submit_order(MarketOrderRequest(
                    symbol=ticker, qty=qty, side=side,
                    time_in_force=TimeInForce.DAY,
                ))
                executed.append(f"{side.value} {qty:.4f} {ticker}")
            except Exception as e:
                errors.append(f"{ticker}: {e}")
                logger.error("Error %s: %s", ticker, e)

    n_adj = len(sells) + len(buys)
    if n_adj > 0 or errors:
        now = datetime.now().strftime("%d-%b %H:%M").lower()
        msg = "\n".join([
            f"⚖️ *REBALANCER* · {now}",
            f"${equity:.0f} · umbral {args.threshold:.0%}",
            "",
            *report_lines,
            *(["", f"✅ {', '.join(executed)}"] if executed else []),
            *(["", f"❌ {', '.join(errors)}"]   if errors   else []),
            *(["\n_(dry-run)_"] if not args.live else []),
        ])
        try:
            send_whatsapp(msg)
        except Exception as e:
            logger.error("WhatsApp error: %s", e)
    else:
        logger.info("Cartera dentro de tolerancia. Sin rebalanceo.")

    logger.info("=== REBALANCER OK === ajustes=%d ejecutados=%d", n_adj, len(executed))


if __name__ == "__main__":
    main()
