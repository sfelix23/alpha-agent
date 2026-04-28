"""
Agente 3 — Monitor intradía de posiciones abiertas.

Corre cada 30 minutos durante el horario de mercado (vía Task Scheduler).
Revisa todas las posiciones abiertas en Alpaca y actúa si:
    - Una posición tocó el stop loss → cierra
    - Una posición tocó el take profit → cierra
    - El equity total cayó más del kill switch % → cierra TODO
    - Una posición subió lo suficiente → trailing stop activado

Sólo manda WhatsApp cuando ACTÚA (cierra algo o detecta alerta crítica).
Si todo está en orden, loguea y sale silenciosamente.

Uso:
    python run_monitor.py              # modo default (dry-run seguro)
    python run_monitor.py --live       # ejecuta cierres reales en Alpaca
    python run_monitor.py --dry-run    # loguea pero no cierra nada
"""

from __future__ import annotations

import sys

# Fix para Windows cp1252: forzar stdout/stderr a UTF-8
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.platform == "win32" and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from alpha_agent.news.claude_analyst import assess_position as claude_assess

# ── Rutas absolutas (funciona sin importar el working directory de Task Scheduler)
BASE_DIR = Path(__file__).parent.resolve()
SIGNALS_PATH = BASE_DIR / "signals" / "latest.json"
LOG_DIR = BASE_DIR / "logs"

logger = logging.getLogger("monitor")


def setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / f"monitor_{datetime.now().strftime('%Y-%m-%d')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(str(log_file), encoding="utf-8"),
        ],
    )


def load_signals_latest() -> dict:
    """Carga el último signals.json para obtener stops/TPs configurados."""
    if not SIGNALS_PATH.exists():
        logger.warning("signals/latest.json no encontrado en %s", SIGNALS_PATH)
        return {}
    try:
        return json.loads(SIGNALS_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Error leyendo signals: %s", e)
        return {}


def get_signal_for_ticker(signals_data: dict, ticker: str) -> dict | None:
    """Busca la señal del ticker en LP, CP, options."""
    for bucket in ("long_term", "short_term", "options_book", "hedge_book"):
        for sig in signals_data.get(bucket, []):
            if sig.get("ticker") == ticker:
                return sig
    return None


def current_price_from_position(pos) -> float:
    """
    Deriva el precio actual desde market_value / qty.
    Position tiene: ticker, qty, avg_price, market_value, unrealized_pl, asset_class
    """
    if pos.qty and abs(pos.qty) > 0:
        return pos.market_value / pos.qty
    return pos.avg_price  # fallback


def pnl_pct_from_position(pos) -> float:
    """Retorna el P&L % como número (ej: 5.3 = +5.3%)"""
    cost_basis = pos.avg_price * pos.qty
    if cost_basis and abs(cost_basis) > 0:
        return (pos.unrealized_pl / cost_basis) * 100.0
    return 0.0


def _update_trailing_stop(
    broker,
    ticker: str,
    new_stop: float,
    qty: float,
    live: bool,
    alerts: list,
    description: str,
) -> None:
    """Actualiza el stop loss en Alpaca y registra la alerta."""
    logger.info("Trailing stop %s: %s", ticker, description)
    alerts.append(f"📈 *{ticker}* trailing stop actualizado — {description}")
    if live:
        oid = broker.update_stop_loss(ticker, new_stop, qty)
        if oid:
            alerts.append(f"  ✅ Stop order enviado (id={oid})")
        else:
            alerts.append(f"  ⚠️ No se pudo enviar stop order")
    else:
        alerts.append("  _(dry-run: stop no enviado a Alpaca)_")


def _compute_chandelier_stop(ticker: str) -> float | None:
    """
    Chandelier Exit = highest_close(22) - 3 × ATR(22).
    Descarga últimos 30 días de OHLC via yfinance (rápido, sin cache).
    Retorna None si no hay suficientes datos.
    """
    try:
        import numpy as np
        import yfinance as yf
        df = yf.download(ticker, period="35d", progress=False, auto_adjust=True)
        if df is None or len(df) < 23:
            return None
        close = df["Close"].squeeze()
        high  = df["High"].squeeze()
        low   = df["Low"].squeeze()
        # ATR(22)
        prev_close = close.shift(1)
        tr = (
            (high - low).abs()
            .combine((high - prev_close).abs(), max)
            .combine((low - prev_close).abs(), max)
        )
        atr22 = float(tr.rolling(22).mean().iloc[-1])
        highest_close_22 = float(close.tail(22).max())
        if np.isnan(atr22) or np.isnan(highest_close_22):
            return None
        return round(highest_close_22 - 3.0 * atr22, 2)
    except Exception as e:
        logger.debug("chandelier_stop %s: %s", ticker, e)
        return None


def _scan_next_cp_opportunity(closed_ticker: str, pnl_pct: float) -> str | None:
    """
    Después de un cierre por TP en CP, busca inmediatamente el próximo trade.
    Corre el Discovery Agent rápido y devuelve el mejor candidato.
    Capital rotation: el capital no duerme ni una hora.
    """
    try:
        from alpha_agent.discovery.screener import run_discovery
        next_picks = run_discovery(max_new=3)
        if not next_picks:
            return None
        best = next_picks[0]
        logger.info("Capital rotation: %s cerró en TP → próxima oportunidad: %s", closed_ticker, best)
        gain_str = f"+{pnl_pct:.1f}%" if pnl_pct > 0 else f"{pnl_pct:.1f}%"
        return (
            f"\n🔄 *CAPITAL ROTATION*\n"
            f"  {closed_ticker} cerró con {gain_str}\n"
            f"  Próximo CP candidato: *{best}*"
            + (f" | también: {', '.join(next_picks[1:])}" if len(next_picks) > 1 else "")
            + "\n  Capital disponible para reutilizar hoy."
        )
    except Exception as e:
        logger.debug("_scan_next_cp_opportunity: %s", e)
        return None


def main():
    parser = argparse.ArgumentParser(description="Monitor intradía de posiciones.")
    parser.add_argument("--live", action="store_true", help="Ejecutar cierres reales en Alpaca.")
    parser.add_argument("--dry-run", action="store_true", help="Loguear sin ejecutar.")
    args = parser.parse_args()

    load_dotenv(BASE_DIR / ".env")
    setup_logging()

    logger.info("=== MONITOR START === live=%s dry_run=%s", args.live, args.dry_run)

    from trader_agent.brokers.alpaca_broker import AlpacaBroker
    from alpha_agent.notifications import send_whatsapp
    from alpha_agent.config import PARAMS

    broker = AlpacaBroker(paper=True)

    # ── 1. Check horario de mercado (salir temprano si el mercado está cerrado)
    try:
        market_open = broker.is_market_open()
    except Exception as e:
        logger.warning("No se pudo verificar horario de mercado: %s. Continuando igual.", e)
        market_open = True  # asumir abierto si falla la consulta

    if not market_open:
        logger.info("Mercado cerrado en este momento. Monitor sale sin acción.")
        return

    # ── 2. Estado actual
    try:
        equity = broker.get_equity()
        positions = broker.get_positions()
    except Exception as e:
        logger.error("Error conectando a Alpaca: %s", e)
        return

    logger.info("💰 Equity: $%.2f | Posiciones: %d", equity, len(positions))

    if not positions:
        logger.info("Sin posiciones abiertas. Nada que monitorear.")
        return

    # ── 3. Cargar stops/TPs del último análisis
    signals_data = load_signals_latest()
    capital_base = signals_data.get("capital_usd", PARAMS.paper_capital_usd)

    # ── 4. Kill switch: equity cayó más del threshold desde el capital base
    kill_switch_pct = 0.03  # -3%
    drawdown = (capital_base - equity) / capital_base if capital_base > 0 else 0

    alerts: list[str] = []
    closes: list[str] = []

    # ── 5. KILL SWITCH CHECK
    if drawdown >= kill_switch_pct:
        logger.warning("🚨 KILL SWITCH activado! DD=%.2f%% (threshold=%.0f%%)",
                       drawdown * 100, kill_switch_pct * 100)
        alerts.append(
            f"🚨 *KILL SWITCH* activado\n"
            f"Equity: ${equity:.0f} (DD {drawdown*100:.1f}% desde ${capital_base:.0f})"
        )

        if args.live and not args.dry_run:
            logger.warning("Cerrando TODAS las posiciones...")
            try:
                broker._trading.close_all_positions(cancel_orders=True)
                closes.append("ALL (kill switch)")
                alerts.append("✅ Todas las posiciones cerradas.")
            except Exception as e:
                alerts.append(f"❌ Error cerrando posiciones: {e}")
                logger.error("Error en kill switch: %s", e)
        else:
            alerts.append("_(dry-run: no se cerraron posiciones)_")

    else:
        # ── 5b. EOD DT close: cerrar posiciones DT antes del cierre de mercado
        # NYSE cierra 16:00 EDT. Forzamos cierre DT a las 15:00 EDT = 19:00 UTC
        # para asegurarnos de no tener overnight en el sleeve de day trading.
        from datetime import timezone as _tz
        _now_utc = datetime.now(_tz.utc)
        if _now_utc.hour >= 19:
            try:
                from alpha_agent.analytics.trade_db import get_open_dt_tickers
                dt_open = get_open_dt_tickers()
                if dt_open:
                    logger.info("EOD DT close: cerrando %d posicion(es) DT %s", len(dt_open), sorted(dt_open))
                    for pos in positions:
                        if pos.ticker not in dt_open:
                            continue
                        current_dt = current_price_from_position(pos)
                        pnl_dt = pos.unrealized_pl
                        pnl_pct_dt = pnl_pct_from_position(pos)
                        logger.info(
                            "EOD DT: cerrando %s | P&L $%+.2f (%+.1f%%)",
                            pos.ticker, pnl_dt, pnl_pct_dt,
                        )
                        alerts.append(
                            f"EOD DT CLOSE *{pos.ticker}* | P&L ${pnl_dt:+.2f} ({pnl_pct_dt:+.1f}%)"
                        )
                        if args.live and not args.dry_run:
                            try:
                                from alpaca.trading.requests import MarketOrderRequest
                                from alpaca.trading.enums import OrderSide, TimeInForce
                                order = MarketOrderRequest(
                                    symbol=pos.ticker,
                                    qty=abs(pos.qty),
                                    side=OrderSide.SELL,
                                    time_in_force=TimeInForce.DAY,
                                )
                                broker._trading.submit_order(order)
                                closes.append(f"{pos.ticker} (EOD-DT)")
                                alerts.append(f"  SELL {abs(pos.qty):.0f} {pos.ticker} enviado")
                                from alpha_agent.analytics.trade_db import log_trade_close
                                log_trade_close(
                                    ticker=pos.ticker,
                                    exit_price=round(current_dt, 2),
                                    pnl_usd=round(pnl_dt, 2),
                                    pnl_pct=round(pnl_pct_dt, 2),
                                )
                            except Exception as _eod_e:
                                logger.error("EOD DT close %s: %s", pos.ticker, _eod_e)
                                alerts.append(f"  ERROR cerrando {pos.ticker}: {_eod_e}")
                        else:
                            alerts.append("  (dry-run: no enviado)")
            except Exception as _eod_err:
                logger.debug("EOD DT check: %s", _eod_err)

        # ── 6. POR POSICIÓN: check stops, TPs, trailing
        for pos in positions:
            ticker = pos.ticker
            avg_entry = pos.avg_price
            qty = pos.qty
            current = current_price_from_position(pos)
            unrealized_pnl = pos.unrealized_pl
            pnl_pct = pnl_pct_from_position(pos)

            signal = get_signal_for_ticker(signals_data, ticker)
            if not signal:
                logger.debug("Sin señal guardada para %s — skip", ticker)
                continue

            stop_loss = signal.get("stop_loss")
            take_profit = signal.get("take_profit")
            macro_regime = signals_data.get("macro", {}).get("regime", "unknown")

            action = None
            reason = ""
            claude_override = False

            # Check stop loss
            stop_loss_hit = bool(stop_loss and current <= stop_loss)
            if stop_loss_hit:
                action = "CLOSE"
                reason = f"STOP LOSS tocado (${current:.2f} <= SL ${stop_loss:.2f})"

            # Check take profit
            elif take_profit and current >= take_profit:
                action = "CLOSE"
                reason = f"TAKE PROFIT alcanzado (${current:.2f} >= TP ${take_profit:.2f})"

            # Chandelier Exit trailing stop (Chuck LeBeau)
            # Sube dinámicamente con el precio; solo cierra si el precio cae
            elif avg_entry > 0:
                chandelier_level = _compute_chandelier_stop(ticker)
                current_sl = signal.get("stop_loss") or 0.0

                if chandelier_level is not None and chandelier_level > current_sl + 0.01:
                    _update_trailing_stop(
                        broker, ticker, chandelier_level, abs(qty), args.live and not args.dry_run,
                        alerts,
                        f"Chandelier Exit @ ${chandelier_level:.2f} (era SL ${current_sl:.2f})",
                    )
                    signal["stop_loss"] = chandelier_level

                # Si el precio actual cae por debajo del chandelier → cerrar
                effective_sl = signal.get("stop_loss")
                if effective_sl and current <= effective_sl:
                    gain_pct = (current - avg_entry) / avg_entry
                    action = "CLOSE"
                    reason = f"CHANDELIER EXIT TOCADO (${current:.2f} <= ${effective_sl:.2f}, P&L {gain_pct*100:+.1f}%)"

            # ── Claude intelligence: consultar cuando estamos cerca del stop ──
            # Si el precio está dentro del 1.5% del stop loss (pero no lo tocó),
            # o si el stop fue tocado, pedimos a Claude que evalúe con contexto noticioso.
            near_stop = (
                stop_loss is not None
                and current > stop_loss
                and (current - stop_loss) / stop_loss < 0.015
            )

            if (action == "CLOSE" or near_stop) and not claude_override:
                news_for_ticker: list[str] = []
                try:
                    thesis = signal.get("thesis", {})
                    news_for_ticker = thesis.get("news", {}).get("headlines", [])
                    if not news_for_ticker:
                        news_for_ticker = []
                except Exception:
                    pass

                claude_result = claude_assess(
                    ticker=ticker,
                    current_price=current,
                    entry_price=avg_entry,
                    pnl_pct=pnl_pct,
                    stop_loss=stop_loss,
                    news_headlines=news_for_ticker,
                    macro_regime=macro_regime,
                )

                if claude_result:
                    claude_action = claude_result["action"]
                    claude_reason = claude_result["reason"]
                    claude_conf = claude_result["confidence"]
                    logger.info(
                        "🤖 Claude sobre %s: %s (conf=%.0f%%) — %s",
                        ticker, claude_action, claude_conf * 100, claude_reason,
                    )

                    if near_stop and action is None:
                        # Stop no tocado pero cerca: Claude puede recomendar cerrar anticipadamente
                        if claude_action == "CLOSE" and claude_conf >= 0.75:
                            action = "CLOSE"
                            reason = f"CIERRE ANTICIPADO por Claude: {claude_reason}"
                            claude_override = True
                        elif claude_action == "REDUCE" and claude_conf >= 0.70:
                            action = "REDUCE"
                            reason = f"REDUCCIÓN por Claude: {claude_reason}"
                            claude_override = True
                    elif action == "CLOSE":
                        if stop_loss_hit:
                            # Stop duro tocado: regla de riesgo inviolable — Claude NO puede vetar
                            if claude_action == "HOLD":
                                logger.info(
                                    "🤖 Claude recomendaba HOLD para %s pero el stop duro es inviolable — cerrando",
                                    ticker,
                                )
                                alerts.append(
                                    f"🤖 *{ticker}*: stop alcanzado — cerrando "
                                    f"(Claude sugería HOLD: {claude_reason})"
                                )
                            # action permanece "CLOSE" — sin veto posible
                        else:
                            # TP o trailing stop: Claude puede vetar (son profit-taking, más flexible)
                            if claude_action == "HOLD" and claude_conf >= 0.80:
                                logger.warning(
                                    "🤖 Claude veta TP/trailing de %s (conf=%.0f%%) — %s",
                                    ticker, claude_conf * 100, claude_reason,
                                )
                                alerts.append(
                                    f"🤖 *{ticker}*: TP/trailing tocado, Claude recomienda HOLD "
                                    f"(conf={claude_conf:.0%}) — {claude_reason}"
                                )
                                action = None  # Solo se veta TP/trailing, nunca el stop duro

            if action in ("CLOSE", "REDUCE"):
                log_msg = f"⚠️ {ticker}: {reason} | P&L: ${unrealized_pnl:+.2f} ({pnl_pct:+.1f}%)"
                logger.warning(log_msg)
                alerts.append(f"⚠️ *{ticker}*: {reason} | P&L ${unrealized_pnl:+.2f}")

                # Flag si el cierre es por TP (capital liberado listo para rotar)
                is_tp_close = action == "CLOSE" and take_profit and current >= take_profit

                if args.live and not args.dry_run:
                    try:
                        from alpaca.trading.requests import MarketOrderRequest
                        from alpaca.trading.enums import OrderSide, TimeInForce
                        sell_qty = abs(qty) if action == "CLOSE" else abs(qty) / 2
                        order = MarketOrderRequest(
                            symbol=ticker,
                            qty=sell_qty,
                            side=OrderSide.SELL,
                            time_in_force=TimeInForce.DAY,
                        )
                        broker._trading.submit_order(order)
                        label = "SELL" if action == "CLOSE" else "SELL HALF"
                        closes.append(f"{ticker} ({reason[:30]})")
                        alerts.append(f"  ✅ {label} enviado: {sell_qty:.4f} {ticker}")

                        # Registrar cierre en SQLite para P&L real
                        try:
                            from alpha_agent.analytics.trade_db import log_trade_close
                            closed_pnl = unrealized_pnl if action == "CLOSE" else unrealized_pnl / 2
                            closed_pct = pnl_pct if action == "CLOSE" else pnl_pct / 2
                            log_trade_close(
                                ticker=ticker,
                                exit_price=current,
                                pnl_usd=round(closed_pnl, 2),
                                pnl_pct=round(closed_pct, 2),
                            )
                        except Exception as _db_e:
                            logger.debug("trade_db close log error: %s", _db_e)

                        # Capital rotation: cuando cierra por TP buscar inmediatamente el próximo trade
                        if is_tp_close:
                            try:
                                _rotation_alert = _scan_next_cp_opportunity(ticker, pnl_pct)
                                if _rotation_alert:
                                    alerts.append(_rotation_alert)
                            except Exception as _re:
                                logger.debug("Capital rotation scan: %s", _re)
                    except Exception as e:
                        alerts.append(f"  ❌ Error cerrando {ticker}: {e}")
                        logger.error("Error cerrando %s: %s", ticker, e)
                else:
                    alerts.append("  _(dry-run: orden no enviada)_")

            else:
                # Posición OK
                logger.info(
                    "✅ %s: $%.2f (entry $%.2f) | P&L $%+.2f (%+.1f%%) — OK",
                    ticker, current, avg_entry, unrealized_pnl, pnl_pct,
                )

    # ── 7. REPORTE
    if alerts:
        now = datetime.now().strftime("%H:%M")
        header = f"🔔 *MONITOR* · {now} · ${equity:.0f}"
        body = "\n".join(alerts)
        footer_parts = [f"\nPosiciones abiertas: {len(positions)}"]
        if closes:
            footer_parts.append(f"Cerradas: {', '.join(closes)}")
        footer = "\n".join(footer_parts)

        msg = f"{header}\n{body}{footer}"
        logger.info("Enviando alerta WhatsApp (%d chars)...", len(msg))
        try:
            send_whatsapp(msg)
        except Exception as e:
            logger.error("Error enviando WhatsApp: %s", e)
    else:
        logger.info("📊 Todas las posiciones dentro de rango. Sin alertas.")

    logger.info("=== MONITOR OK ===")


if __name__ == "__main__":
    main()
