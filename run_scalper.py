"""
Scalper autónomo — Opening Range Breakout con WebSocket de Alpaca.

Estrategia:
  9:30-9:45 EDT → construye el rango de apertura (ORB) de 15 min
  9:45-15:45 EDT → busca breakouts con volumen confirmado
  Breakout detectado → Swarm 2-agentes valida → bracket order automático
  15:45 EDT → cierre forzado de todas las posiciones scalp

Parámetros:
  Budget:  $400 por trade
  SL:      0.3-0.5% (borde opuesto del rango)
  TP:      0.4-1.5% (1.5× el tamaño del rango)
  R/R:     ~2:1
  Máx:     4 trades/día
  Cuenta:  ALPACA_SCALP_API_KEY / ALPACA_SCALP_SECRET_KEY  ← cuenta separada

IMPORTANTE: Este script debe correr como proceso continuo durante el horario
de mercado. NO es compatible con GitHub Actions (cron tiene latencia de ~30-60s).

Cómo ejecutar:
  python run_scalper.py [--dry-run]    # durante mercado abierto
  python run_scalper.py --live         # con órdenes reales

Para correr en background (Windows):
  Start-Process python -ArgumentList "run_scalper.py --live" -WindowStyle Hidden
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).parent.resolve()

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.platform == "win32" and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env")

log = logging.getLogger("scalper")
ET = ZoneInfo("America/New_York")


def _setup_logging() -> None:
    today    = datetime.now().strftime("%Y-%m-%d")
    log_file = BASE_DIR / "logs" / f"scalper_{today}.log"
    log_file.parent.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(str(log_file), encoding="utf-8"),
        ],
    )


def _build_broker():
    """Broker exclusivo de la cuenta SCALP — separada de LP/CP y DT."""
    api_key = os.getenv("ALPACA_SCALP_API_KEY")
    secret  = os.getenv("ALPACA_SCALP_SECRET_KEY")
    if not (api_key and secret):
        raise RuntimeError(
            "ALPACA_SCALP_API_KEY / ALPACA_SCALP_SECRET_KEY no configuradas. "
            "Crear nueva cuenta Alpaca paper y agregar al .env"
        )
    os.environ["ALPACA_API_KEY"]    = api_key
    os.environ["ALPACA_SECRET_KEY"] = secret
    from trader_agent.brokers.alpaca_broker import AlpacaBroker
    broker = AlpacaBroker(paper=True)
    log.info("SCALP broker → cuenta separada (%s...)", api_key[:8])
    return broker


def _place_bracket(broker, ticker: str, bracket: dict, live: bool) -> str | None:
    """Submite bracket order (market + SL + TP)."""
    direction = bracket["direction"]
    qty       = bracket["qty"]
    sl        = bracket["sl"]
    tp        = bracket["tp"]

    if not live:
        log.info("[DRY-RUN] %s %d %s SL=%.2f TP=%.2f", direction, qty, ticker, sl, tp)
        return "dry-run"

    try:
        from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
        from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
        side = OrderSide.BUY if direction == "LONG" else OrderSide.SELL
        req  = MarketOrderRequest(
            symbol=ticker, qty=qty, side=side,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=round(tp, 2)),
            stop_loss=StopLossRequest(stop_price=round(sl, 2)),
        )
        order = broker._trading.submit_order(req)
        log.info("ORDER SUBMITTED: %s %s %d@mkt SL=%.2f TP=%.2f id=%s",
                 direction, ticker, qty, sl, tp, order.id)
        return str(order.id)
    except Exception as e:
        log.error("bracket_order %s: %s", ticker, e)
        return None


def _close_all_scalp_positions(broker, live: bool) -> None:
    """Cierre EOD forzado de todas las posiciones scalp abiertas."""
    try:
        positions = broker.get_positions()
        for pos in positions:
            ticker = pos.ticker
            qty    = abs(int(float(pos.qty)))
            if qty == 0:
                continue
            if not live:
                log.info("[DRY-RUN] EOD close %s %d shares", ticker, qty)
                continue
            try:
                broker._trading.close_position(ticker)
                log.info("EOD CLOSE: %s", ticker)
            except Exception as e:
                log.warning("EOD close %s: %s", ticker, e)
    except Exception as e:
        log.warning("get_positions: %s", e)


async def _stream_and_trade(live: bool) -> None:
    """
    Corre el stream WebSocket de Alpaca y procesa los bars 1-min.
    """
    from alpaca.data.live import StockDataStream
    from alpaca.data.models import Bar
    from alpha_agent.scalping.orb_strategy import (
        SCALP_UNIVERSE, ORBState,
        compute_bracket, is_eod, is_in_orb_window, is_in_trading_window,
    )
    from alpha_agent.scalping.swarm_scalp import validate_scalp

    api_key = os.getenv("ALPACA_SCALP_API_KEY", "")
    secret  = os.getenv("ALPACA_SCALP_SECRET_KEY", "")

    orb_states:   dict[str, ORBState] = {t: ORBState(ticker=t) for t in SCALP_UNIVERSE}
    trades_today: int                 = 0
    broker = _build_broker()

    # VIX para el Swarm (no cambia en el loop)
    vix = 20.0
    try:
        import yfinance as yf
        vix = float(yf.download("^VIX", period="1d", interval="1m", progress=False)["Close"].iloc[-1])
        log.info("VIX actual: %.1f", vix)
    except Exception:
        pass

    log.info("=== SCALPER START === tickers=%d live=%s VIX=%.1f", len(SCALP_UNIVERSE), live, vix)

    wss = StockDataStream(api_key, secret)

    async def handle_bar(bar: Bar) -> None:
        nonlocal trades_today
        ticker = bar.symbol
        state  = orb_states.get(ticker)
        if not state:
            return

        now_et   = datetime.now(ET)
        et_hour  = now_et.hour
        et_min   = now_et.minute
        bar_dict = {
            "o": float(bar.open), "h": float(bar.high),
            "l": float(bar.low),  "c": float(bar.close),
            "v": float(bar.volume),
        }

        # EOD: cerrar todo y salir
        if is_eod(et_hour, et_min):
            if not state.traded:
                pass  # nada abierto
            return

        # Ventana ORB: construir el rango
        if is_in_orb_window(et_hour, et_min) and not state.locked:
            state.update(bar_dict)
            log.debug("%s ORB update: H=%.2f L=%.2f range=%.2f%%",
                      ticker, state.orb_high, state.orb_low, state.range_pct * 100)
            return

        # Lockear el rango cuando salimos de la ventana ORB
        if not state.locked and et_hour == 9 and et_min >= 45:
            state.locked = True
            if state.is_valid():
                log.info("%s ORB LOCKED: H=%.2f L=%.2f range=%.2f%%",
                         ticker, state.orb_high, state.orb_low, state.range_pct * 100)
            else:
                log.debug("%s ORB inválido (range=%.2f%%) — skip", ticker, state.range_pct * 100)

        # Ventana de trading: buscar breakout
        if not is_in_trading_window(et_hour, et_min):
            return
        if state.traded or not state.locked or not state.is_valid():
            return
        if trades_today >= 4:
            return

        direction = state.check_breakout(bar_dict)
        if not direction:
            return

        entry   = bar_dict["c"]
        bracket = compute_bracket(direction, entry, state)

        log.info(
            "BREAKOUT %s %s @ %.2f | ORB %.2f%% | R/R %.2f:1",
            ticker, direction, entry, bracket["range_pct"], bracket["rr"],
        )

        # Swarm validation
        go, reason = validate_scalp(
            ticker=ticker, direction=direction, bracket=bracket,
            orb_range_pct=state.range_pct * 100,
            trades_today=trades_today, vix=vix,
        )

        if not go:
            log.info("SCALP VETADO %s — %s", ticker, reason)
            try:
                from alpha_agent.notifications import send_whatsapp
                send_whatsapp(f"SCALP VETO {ticker} [{direction}]\n{reason[:200]}")
            except Exception:
                pass
            return

        oid = _place_bracket(broker, ticker, bracket, live)
        if oid:
            state.traded  = True
            trades_today += 1
            log.info(
                "SCALP EJECUTADO: %s %s %d shares @ $%.2f "
                "SL=$%.2f TP=$%.2f R/R=%.2f:1 [%d/4 hoy]",
                direction, ticker, bracket["qty"], entry,
                bracket["sl"], bracket["tp"], bracket["rr"], trades_today,
            )
            try:
                from alpha_agent.analytics.trade_db import log_trade
                log_trade(
                    ticker=ticker,
                    side="BUY" if direction == "LONG" else "SELL",
                    qty=float(bracket["qty"]),
                    price=entry,
                    notional=bracket["notional"],
                    sleeve="SCALP",
                    status="filled" if live else "dry-run",
                    order_id=oid,
                    stop_loss=bracket["sl"],
                    take_profit=bracket["tp"],
                    vix=vix,
                )
            except Exception as e_db:
                log.warning("trade_db scalp: %s", e_db)

            try:
                from alpha_agent.notifications import send_whatsapp
                now_str = datetime.now().strftime("%H:%M")
                send_whatsapp(
                    f"SCALP | {now_str} | VIX {vix:.1f}\n"
                    f"{direction} {ticker} {bracket['qty']} shares @ ${entry:.2f}\n"
                    f"SL ${bracket['sl']:.2f} (-{bracket['sl_pct']:.1f}%) | "
                    f"TP ${bracket['tp']:.2f} (+{bracket['tp_pct']:.1f}%)\n"
                    f"R/R {bracket['rr']:.2f}:1 | ORB {bracket['range_pct']:.2f}%\n"
                    f"Swarm: {reason[:120]}\n"
                    f"Cierre EOD 15:45 EDT"
                )
            except Exception:
                pass

    # Suscribir todos los tickers
    for ticker in SCALP_UNIVERSE:
        wss.subscribe_bars(handle_bar, ticker)

    log.info("WebSocket iniciado. Esperando bars de %s...", ", ".join(SCALP_UNIVERSE))
    await wss._run_forever()


async def _eod_watchdog(broker, live: bool) -> None:
    """
    Corre en paralelo con el stream y vigila el cierre EOD (15:45 ET).
    """
    from alpha_agent.scalping.orb_strategy import is_eod
    while True:
        await asyncio.sleep(30)
        now_et  = datetime.now(ET)
        if is_eod(now_et.hour, now_et.minute):
            log.info("EOD WATCHDOG: cerrando posiciones SCALP...")
            _close_all_scalp_positions(broker, live)
            log.info("=== SCALPER EOD DONE ===")
            return


def main() -> None:
    parser = argparse.ArgumentParser(description="Scalper ORB — proceso continuo.")
    parser.add_argument("--live",    action="store_true", help="Órdenes reales en Alpaca DT.")
    parser.add_argument("--dry-run", action="store_true", help="Solo logear, sin ejecutar.")
    args = parser.parse_args()
    live = args.live and not args.dry_run

    # Fix WinError 121 (semáforo timeout) en Windows con asyncio WebSocket.
    # ProactorEventLoop (default) tiene bugs con conexiones WebSocket largas;
    # SelectorEventLoop es más estable para I/O de red de larga duración.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.SelectorEventLoopPolicy())

    _setup_logging()
    log.info("=== SCALPER INIT === live=%s", live)

    try:
        broker = _build_broker()
    except RuntimeError as e:
        log.error("%s", e)
        return

    try:
        if not broker.is_market_open():
            log.info("Mercado cerrado. Scalper sale.")
            return
    except Exception:
        pass

    async def _main_async():
        stream_task    = asyncio.create_task(_stream_and_trade(live))
        watchdog_task  = asyncio.create_task(_eod_watchdog(broker, live))
        done, pending  = await asyncio.wait(
            [stream_task, watchdog_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()

    asyncio.run(_main_async())
    log.info("=== SCALPER FINISH ===")


if __name__ == "__main__":
    main()
