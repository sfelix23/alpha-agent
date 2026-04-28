"""
Agente Day Trader -- cuenta Alpaca SEPARADA, estrategia concentrada.

Capital: cuenta DT propia (~$1600 paper), 1 sola posicion por dia.
Budget deployado: $1400 (87.5%), reserva $200 buffer.

Cuenta LP/CP -> ALPACA_API_KEY / ALPACA_SECRET_KEY     (existente)
Cuenta DT    -> ALPACA_DT_API_KEY / ALPACA_DT_SECRET_KEY  (nueva, pendiente)

Setup: gap alcista +1.5% + precio > VWAP + volumen x1.5 + RSI 42-74.
Bracket fijo: SL -1.5%, TP +3.5% (R/R 2.33:1).
EOD close automatico a las 15:00 EDT via run_monitor.py.
"""

from __future__ import annotations

import sys

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.platform == "win32" and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import argparse
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent.resolve()
LOG_DIR  = BASE_DIR / "logs"

logger = logging.getLogger("daytrader")

# Desplegamos 87.5% del capital DT en la mejor idea del dia.
# Con $1600 en la cuenta => $1400 por trade.
# Ej: AMD $120 => 11 shares; GOOGL $165 => 8 shares; COIN $220 => 6 shares.
DT_BUDGET  = 1400.0
DT_MAX_POS = 1       # 1 sola posicion concentrada por dia

# Ventana de entrada: 10:00-14:00 EDT = 14:00-18:00 UTC (verano)
ENTRY_OPEN_UTC  = 14
ENTRY_CLOSE_UTC = 18


def _setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = LOG_DIR / ("daytrader_" + today + ".log")
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


def _build_dt_broker():
    """
    Broker apuntando a la cuenta DT (keys ALPACA_DT_*).
    Fallback a ALPACA_* si las DT keys no existen (dev / dry-run).
    """
    api_key = os.getenv("ALPACA_DT_API_KEY") or os.getenv("ALPACA_API_KEY")
    secret  = os.getenv("ALPACA_DT_SECRET_KEY") or os.getenv("ALPACA_SECRET_KEY")
    if not (api_key and secret):
        raise RuntimeError("Faltan ALPACA_DT_API_KEY / ALPACA_DT_SECRET_KEY en .env")

    # Inyectamos las keys DT para que AlpacaBroker las lea
    os.environ["ALPACA_API_KEY"]    = api_key
    os.environ["ALPACA_SECRET_KEY"] = secret

    from trader_agent.brokers.alpaca_broker import AlpacaBroker
    broker = AlpacaBroker(paper=True)
    logger.info("DT broker -> cuenta separada (%s...)", api_key[:8])
    return broker


def _count_open_dt() -> int:
    """Posiciones DT abiertas hoy segun trade_db."""
    try:
        from alpha_agent.analytics.trade_db import get_trades
        today  = datetime.now().strftime("%Y-%m-%d")
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
    parser = argparse.ArgumentParser(description="Day Trader -- cuenta DT separada.")
    parser.add_argument("--live",    action="store_true", help="Ordenes reales en Alpaca DT.")
    parser.add_argument("--dry-run", action="store_true", help="Loguear sin ejecutar.")
    args = parser.parse_args()
    live = args.live and not args.dry_run

    load_dotenv(BASE_DIR / ".env")
    _setup_logging()

    logger.info("=== DAYTRADER START === live=%s budget=$%.0f", live, DT_BUDGET)

    if not _in_entry_window():
        logger.info("Fuera de ventana DT (10:00-14:00 EDT). Saliendo.")
        return

    broker = _build_dt_broker()

    try:
        if not broker.is_market_open():
            logger.info("Mercado cerrado. DT sale.")
            return
    except Exception as e:
        logger.warning("is_market_open: %s -- asumiendo abierto", e)

    if _count_open_dt() >= DT_MAX_POS:
        logger.info("Ya hay 1 posicion DT abierta hoy. Sin nueva entrada.")
        return

    held = _held_tickers(broker)

    from alpha_agent.daytrading.scanner import scan_dt_candidates
    logger.info("Escaneando universo DT (budget=$%.0f)...", DT_BUDGET)
    candidates = scan_dt_candidates(
        exclude_tickers=held,
        budget_per_pos=DT_BUDGET,
        limit=1,
    )

    if not candidates:
        logger.info("Sin setup DT valido hoy. Saliendo.")
        return

    cand     = candidates[0]
    ticker   = cand["ticker"]
    price    = cand["current_price"]
    qty      = cand["qty_shares"]
    notional = cand["notional"]
    sl       = cand["stop_loss"]
    tp       = cand["take_profit"]
    rr       = (tp - price) / (price - sl) if (price - sl) > 0 else 0.0

    regime, vix = "UNKNOWN", 0.0
    try:
        from alpha_agent.macro.macro_context import fetch_macro_snapshot
        macro  = fetch_macro_snapshot()
        regime = macro.regime
        vix    = float(macro.prices.get("vix", 0.0))
    except Exception:
        pass

    logger.info(
        "DT ENTRADA: %s | score=%.3f | %d x $%.2f = $%.0f | SL $%.2f TP $%.2f R/R %.1f:1",
        ticker, cand["dt_score"], qty, price, notional, sl, tp, rr,
    )

    order_id = _bracket_order(broker, ticker, qty, sl, tp, live)
    if order_id is None:
        logger.error("DT: fallo bracket order %s. Saliendo.", ticker)
        return

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

    from alpha_agent.notifications import send_whatsapp
    ts      = datetime.now().strftime("%H:%M")
    gap_pct = cand["gap_pct"]
    vol_r   = cand["vol_ratio"]
    rsi_v   = cand["rsi"]
    msg = (
        "DAY TRADER | " + ts + " | " + regime + " | VIX " + str(round(vix, 1)) + "\n"
        + "ENTRADA " + ticker + "\n"
        + "  $" + str(round(price, 2)) + " x " + str(qty) + " = $" + str(round(notional)) + "\n"
        + "  SL $" + str(sl) + " (-1.5%) | TP $" + str(tp) + " (+3.5%) | R/R " + str(round(rr, 1)) + ":1\n"
        + "  gap=" + str(round(gap_pct * 100, 1)) + "% "
        + "vol=" + str(round(vol_r, 1)) + "x "
        + "RSI=" + str(round(rsi_v)) + "\n"
        + "  Cierre EOD automatico 15:00 EDT"
    )
    logger.info("WhatsApp DT (%d chars)...", len(msg))
    try:
        send_whatsapp(msg)
    except Exception as e:
        logger.error("WhatsApp DT: %s", e)

    logger.info("=== DAYTRADER OK === %s %d shares $%.0f", ticker, qty, notional)


if __name__ == "__main__":
    main()
