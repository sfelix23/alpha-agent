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
    api_key = os.getenv("ALPACA_DT_API_KEY")
    secret  = os.getenv("ALPACA_DT_SECRET_KEY")
    if not (api_key and secret):
        logger.warning(
            "ALPACA_DT_API_KEY / ALPACA_DT_SECRET_KEY no configuradas. "
            "Agregar al .env cuando tengas la nueva cuenta Alpaca paper."
        )
        raise RuntimeError("DT keys ausentes — agrega ALPACA_DT_API_KEY al .env")

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


def _bracket_order(broker, ticker: str, qty: int, sl: float, tp: float,
                   live: bool, direction: str = "LONG") -> str | None:
    """Bracket order individual — soporta LONG (BUY) y SHORT (SELL)."""
    side_label = "BUY" if direction == "LONG" else "SHORT"
    if not live:
        logger.info("[DRY-RUN] %s %d %s | SL=%.2f TP=%.2f", side_label, qty, ticker, sl, tp)
        return "dry-run"
    try:
        from alpaca.trading.requests import (
            MarketOrderRequest, TakeProfitRequest, StopLossRequest,
        )
        from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

        side = OrderSide.BUY if direction == "LONG" else OrderSide.SELL
        req  = MarketOrderRequest(
            symbol=ticker,
            qty=qty,
            side=side,
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


def _dual_bracket(broker, ticker: str, qty1: int, qty2: int, sl: float,
                  tp1: float, tp2: float, live: bool,
                  direction: str = "LONG") -> tuple[str | None, str | None]:
    """Dual bracket: dos ordenes independientes. Soporta LONG y SHORT."""
    oid1 = _bracket_order(broker, ticker, qty1, sl, tp1, live, direction) if qty1 >= 1 else None
    oid2 = _bracket_order(broker, ticker, qty2, sl, tp2, live, direction) if qty2 >= 1 else None
    return oid1, oid2


def _get_macro_ctx(macro) -> dict:
    """Extrae dict plano del macro snapshot para el Decision Committee."""
    try:
        pr = macro.prices or {}
        return {
            "regime":      macro.regime,
            "vix":         float(pr.get("vix", 0) or 0),
            "wti":         float(pr.get("oil_wti", 0) or 0),
            "dxy":         float(pr.get("dxy", 0) or 0),
            "gold":        float(pr.get("gold", 0) or 0),
            "spy_vwap_dev": 0.0,  # el scanner ya aplicó el filtro
        }
    except Exception:
        return {}


def main() -> None:
    parser = argparse.ArgumentParser(description="Day Trader -- cuenta DT separada.")
    parser.add_argument("--live",       action="store_true", help="Ordenes reales en Alpaca DT.")
    parser.add_argument("--dry-run",    action="store_true", help="Loguear sin ejecutar.")
    parser.add_argument("--no-committee", action="store_true",
                        help="Saltar el Decision Committee (modo reglas puras).")
    args = parser.parse_args()
    live = args.live and not args.dry_run

    load_dotenv(BASE_DIR / ".env")
    _setup_logging()

    logger.info("=== DAYTRADER START === live=%s budget=$%.0f", live, DT_BUDGET)

    if not _in_entry_window():
        logger.info("Fuera de ventana DT (10:00-14:00 EDT). Saliendo.")
        return

    try:
        broker = _build_dt_broker()
    except RuntimeError as e:
        logger.warning("DT no configurado: %s", e)
        return

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

    cand      = candidates[0]
    ticker    = cand["ticker"]
    direction = cand.get("direction", "LONG")
    price     = cand["current_price"]
    qty       = cand["qty_shares"]
    qty1      = cand["qty1"]
    qty2      = cand["qty2"]
    notional  = cand["notional"]
    sl        = cand["stop_loss"]
    tp1       = cand["take_profit_1"]
    tp2       = cand["take_profit_2"]

    # ── Macro snapshot ─────────────────────────────────────────────────────
    regime, vix, macro_ctx = "UNKNOWN", 0.0, {}
    try:
        from alpha_agent.macro.macro_context import fetch_macro_snapshot
        macro_snap = fetch_macro_snapshot()
        regime     = macro_snap.regime
        vix        = float(macro_snap.prices.get("vix", 0.0))
        macro_ctx  = _get_macro_ctx(macro_snap)
    except Exception:
        pass

    # ── Polymarket ─────────────────────────────────────────────────────────
    polymarket = {}
    try:
        from alpha_agent.macro.polymarket import fetch_polymarket_signals
        polymarket = fetch_polymarket_signals()
        if polymarket:
            logger.info("Polymarket: %s", " | ".join(f"{k}={v:.0%}" for k, v in polymarket.items()))
    except Exception as e:
        logger.debug("Polymarket no disponible: %s", e)

    # ── Decision Committee ─────────────────────────────────────────────────
    size_factor  = 1.0
    swarm_msg    = ""
    swarm_ev     = 0.0
    swarm_go_cnt = 0
    if not args.no_committee:
        try:
            from alpha_agent.swarm import evaluate as swarm_eval
            from alpha_agent.analytics.trade_db import get_trades

            recent    = get_trades(limit=10)
            dt_recent = [t for t in recent if t.get("sleeve") == "DT"]
            today_str = datetime.now().strftime("%Y-%m-%d")
            pnl_today    = sum(t.get("pnl_usd") or 0 for t in dt_recent if t.get("date") == today_str)
            trades_today = len([t for t in dt_recent if t.get("date") == today_str])

            decision = swarm_eval(
                candidate=cand,
                direction=direction,
                macro_ctx=macro_ctx,
                portfolio_heat=0.0,
                pnl_today=pnl_today,
                trades_today=trades_today,
                polymarket=polymarket,
            )
            swarm_msg    = decision.reasoning
            swarm_ev     = decision.ev_data.get("ev", 0.0)
            swarm_go_cnt = decision.go_count
            logger.info(
                "Swarm: %s | size=%.2f | GO=%d/4 | EV=$%+.2f | %s",
                "GO" if decision.go else "NO-GO",
                decision.size_factor, decision.go_count, swarm_ev,
                decision.reasoning[:100],
            )

            if not decision.go:
                logger.info("Swarm VETÓ el trade. Saliendo.")
                try:
                    from alpha_agent.notifications import send_whatsapp
                    send_whatsapp(
                        f"DT SWARM VETO {ticker} [{direction}]\n"
                        f"Score cuant: {cand['dt_score']:.3f} — aprobado por reglas\n"
                        f"EV: ${swarm_ev:+.2f} | GO: {swarm_go_cnt}/4\n"
                        f"Razon: {decision.reasoning[:200]}"
                    )
                except Exception:
                    pass
                return

            size_factor = decision.size_factor
            if size_factor < 1.0:
                qty  = max(1, int(qty * size_factor))
                qty1 = qty // 2
                qty2 = qty - qty1
                notional = qty * price
                logger.info("Swarm: size ajustado a %.0f%% → %d shares", size_factor * 100, qty)

        except Exception as e:
            logger.warning("Swarm falló, usando reglas puras: %s", e)

    # ── Log + ejecución ────────────────────────────────────────────────────
    dir_icon = "↑ LONG" if direction == "LONG" else "↓ SHORT"
    logger.info(
        "DT ENTRADA [%s]: %s | score=%.3f | %d shares x $%.2f = $%.0f",
        dir_icon, ticker, cand["dt_score"], qty, price, notional,
    )
    logger.info("  Tramo1: %d shares SL $%.2f TP1 $%.2f", qty1, sl, tp1)
    logger.info("  Tramo2: %d shares SL $%.2f TP2 $%.2f", qty2, sl, tp2)

    oid1, oid2 = _dual_bracket(broker, ticker, qty1, qty2, sl, tp1, tp2, live, direction)
    if oid1 is None and oid2 is None:
        logger.error("DT: fallaron ambos bracket orders para %s. Saliendo.", ticker)
        return

    if live:
        try:
            from alpha_agent.analytics.trade_db import log_trade
            side_db = "BUY" if direction == "LONG" else "SELL"
            log_trade(
                ticker=ticker, side=side_db,
                qty=float(qty), price=price, notional=notional,
                sleeve="DT", status="filled",
                order_id=str(oid1) + "+" + str(oid2),
                stop_loss=sl, take_profit=tp2,
                regime=regime, vix=vix,
            )
        except Exception as e_db:
            logger.warning("trade_db DT: %s", e_db)

    from alpha_agent.notifications import send_whatsapp
    ts      = datetime.now().strftime("%H:%M")
    gap_pct = cand["gap_pct"]
    orb_s   = cand.get("orb_score", 0.0)
    vol_r   = cand["vol_ratio"]
    rsi_v   = cand["rsi"]
    if direction == "LONG":
        rr2 = (tp2 - price) / (price - sl) if (price - sl) > 0 else 0.0
        dir_label = "LONG"
    else:
        rr2 = (price - tp2) / (sl - price) if (sl - price) > 0 else 0.0
        dir_label = "SHORT"

    swarm_line = ""
    if swarm_msg:
        ev_sign = "+" if swarm_ev >= 0 else ""
        swarm_line = (
            f"\n  Swarm [{swarm_go_cnt}/4 GO | EV ${ev_sign}{round(swarm_ev, 1)}]: "
            f"{swarm_msg[:110]}"
        )
    msg = (
        f"DAY TRADER | {ts} | {regime} | VIX {round(vix,1)}\n"
        f"ENTRADA {dir_label} {ticker} ({qty} shares = ${round(notional)})\n"
        f"  Tramo1: {qty1} shares -> TP ${round(tp1,2)} (+3%)\n"
        f"  Tramo2: {qty2} shares -> TP ${round(tp2,2)} (+7%) R/R {round(rr2,1)}:1\n"
        f"  SL ${round(sl,2)} (-1.5%)\n"
        f"  gap={round(gap_pct*100,1)}% ORB={round(orb_s,2)} vol={round(vol_r,1)}x RSI={round(rsi_v)}"
        f"{swarm_line}\n"
        f"  Cierre EOD automatico 15:00 EDT"
    )
    logger.info("WhatsApp DT (%d chars)...", len(msg))
    try:
        send_whatsapp(msg)
    except Exception as e:
        logger.error("WhatsApp DT: %s", e)

    logger.info("=== DAYTRADER OK === [%s] %s %d shares $%.0f", direction, ticker, qty, notional)


if __name__ == "__main__":
    main()
