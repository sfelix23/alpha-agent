"""
Agente Day Trader -- setups intraday de alta liquidez.

Estrategia:
  - Universo: QQQ mega-caps + momentum names (AMD, NVDA, TSLA, COIN, CRWD...)
  - Setup: gap alcista +1.5pct + precio > VWAP + volumen x1.5 + RSI 42-74
  - Entrada: bracket order automatico (stop -1.5pct, TP +3.5pct, R/R 2.33:1)
  - Capital: USD 160 por posicion (max 2 simultáneas)
  - EOD: el monitor cierra todo a las 15:00 EDT, nunca overnight

Corre junto al pipeline LP/CP. Se dispara desde run_autonomous.ps1 paso 3.
"""

from __future__ import annotations

import sys

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.platform == "win32" and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent.resolve()
LOG_DIR  = BASE_DIR / "logs"

logger = logging.getLogger("daytrader")

DT_CAPITAL_PER_POS = 160.0   # USD por posicion
DT_MAX_POSITIONS   = 2       # max 2 DT simultaneas

# Ventana de entrada: 10:00-14:00 EDT = 14:00-18:00 UTC (verano)
ENTRY_OPEN_UTC  = 14
ENTRY_CLOSE_UTC = 18


def _setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = LOG_DIR / f"daytrader_{today}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s -- %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(str(log_file), encoding="utf-8"),
        ],
    )


def _in_entry_window() -> bool:
    hour = datetime.now(tz=timezone.utc).hour
    return ENTRY_OPEN_UTC <= hour < ENTRY_CLOSE_UTC


def _count_open_dt() -> int:
    try:
        from alpha_agent.analytics.trade_db import get_trades
        today = datetime.now().strftime("%Y-%m-%d")
        trades = get_trades(limit=200)
        return sum(
            1 for t in trades
            if t.get("sleeve") == "DT"
            and t.get("side") == "BUY"
            and t.get("closed_at") is None
            and t.get("date") == today
        )
    except Exception as e:
        logger.debug("_count_open_dt: %s", e)
        return 0


def _held_tickers(broker) -> set:
    try:
        return {p.ticker for p in broker.get_positions()}
    except Exception:
        return set()


def _bracket_order(broker, ticker: str, qty: int, sl: float, tp: float, live: bool):
    if not live:
        logger.info("[DRY-RUN] BUY %d %s | SL=%.2f TP=%.2f", qty, ticker, sl, tp)
        return "dry-run"
    try:
        from alpaca.trading.requests import (
            MarketOrderRequest, TakeProfitRequest, StopLossRequest,
        )
        from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

        req = MarketOrderRequest(
            symbol=ticker,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=round(tp, 2)),
            stop_loss=StopLossRequest(stop_price=round(sl, 2)),
        )
        submitted = broker._trading.submit_order(req)
        return str(submitted.id)
    except Exception as e:
        logger.error("bracket_order %s: %s", ticker, e)
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Day Trader intraday.")
    parser.add_argument("--live",    action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--capital", type=float, default=None)
    args = parser.parse_args()
    live = args.live and not args.dry_run

    load_dotenv(BASE_DIR / ".env")
    _setup_logging()

    logger.info("=== DAYTRADER START === live=%s", live)

    if not _in_entry_window():
        logger.info("Fuera de ventana DT (10:00-14:00 EDT). Saliendo.")
        return

    from trader_agent.brokers.alpaca_broker import AlpacaBroker
    from alpha_agent.notifications import send_whatsapp

    broker = AlpacaBroker(paper=True)

    try:
        if not broker.is_market_open():
            logger.info("Mercado cerrado. DT sale.")
            return
    except Exception as e:
        logger.warning("is_market_open: %s -- asumiendo abierto", e)

    open_dt = _count_open_dt()
    if open_dt >= DT_MAX_POSITIONS:
        logger.info("DT lleno: %d/%d posiciones.", open_dt, DT_MAX_POSITIONS)
        return
    slots = DT_MAX_POSITIONS - open_dt

    held = _held_tickers(broker)

    from alpha_agent.daytrading.scanner import scan_dt_candidates
    logger.info("Escaneando universo DT (%d slots)...", slots)
    candidates = scan_dt_candidates(exclude_tickers=held, limit=slots)

    if not candidates:
        logger.info("Sin candidatos DT. Saliendo.")
        return

    regime, vix = "UNKNOWN", 0.0
    try:
        from alpha_agent.macro.macro_context import fetch_macro_snapshot
        macro = fetch_macro_snapshot()
        regime = macro.regime
        vix    = float(macro.prices.get("vix", 0.0))
    except Exception:
        pass

    alerts = []
    executed = 0

    for cand in candidates:
        ticker = cand["ticker"]
        price  = cand["current_price"]
        sl     = cand["stop_loss"]
        tp     = cand["take_profit"]

        qty = int(DT_CAPITAL_PER_POS / price)
        if qty < 1:
            logger.warning("DT %s: precio %.2f > presupuesto %.0f. Skip.", ticker, price, DT_CAPITAL_PER_POS)
            continue

        notional = qty * price
        rr = (tp - price) / (price - sl) if (price - sl) > 0 else 0.0

        logger.info(
            "DT entrada: %s | score=%.3f | %d x USD%.2f = USD%.0f | SL=%.2f TP=%.2f R/R=%.1f:1",
            ticker, cand["dt_score"], qty, price, notional, sl, tp, rr,
        )

        order_id = _bracket_order(broker, ticker, qty, sl, tp, live)
        if order_id is None:
            continue

        if live:
            try:
                from alpha_agent.analytics.trade_db import log_trade
                log_trade(
                    ticker=ticker, side="BUY",
                    qty=float(qty), price=price, notional=notional,
                    sleeve="DT", status="filled", order_id=order_id,
                    stop_loss=sl, take_profit=tp, regime=regime, vix=vix,
                )
            except Exception as e_db:
                logger.warning("trade_db DT: %s", e_db)

        gap_pct  = cand["gap_pct"]
        vol_ratio = cand["vol_ratio"]
        rsi_val  = cand["rsi"]
        alerts.append(
            "DT ENTRY " + ticker + "\n"
            "  USD" + f"{price:.2f}" + " x " + str(qty) + " = USD" + f"{notional:.0f}" + "\n"
            "  SL USD" + f"{sl:.2f}" + " (-1.5%) | TP USD" + f"{tp:.2f}" + " (+3.5%) | R/R " + f"{rr:.1f}:1" + "\n"
            "  gap=" + f"{gap_pct*100:.1f}%" + " vol=" + f"{vol_ratio:.1f}x" + " RSI=" + f"{rsi_val:.0f}"
        )
        executed += 1

    if alerts:
        ts  = datetime.now().strftime("%H:%M")
        msg = (
            f"DAY TRADER | {ts} | {regime} | VIX {vix:.1f}\n"
            + "\n".join(alerts)
            + "\nCierre EOD automatico 15:00 EDT"
        )
        logger.info("WhatsApp DT (%d chars)...", len(msg))
        try:
            send_whatsapp(msg)
        except Exception as e:
            logger.error("WhatsApp DT: %s", e)

    logger.info("=== DAYTRADER OK === ejecutados=%d", executed)


if __name__ == "__main__":
    main()
