"""
Estrategia de ejecución del trader_agent.

Reglas (v1):
    1. Solo opera con mercado abierto (US equity).
    2. Kill switch: si el drawdown del día supera PARAMS.max_daily_drawdown,
       abortamos y no enviamos NINGUNA orden. El equity al abrir el día se
       persiste en .cache/trader_day_state.json.
    3. Lee signals/latest.json.
    4. Construye target portfolio equity (LP + CP) y diff vs broker.
    5. Construye OptionIntents (directionales + hedge) si el broker soporta.
    6. Envía órdenes (equity primero, opciones después).
    7. Avisa por WhatsApp con resumen de fills.

Flags:
    - dry_run: si True, imprime las órdenes sin enviarlas.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path

from alpha_agent.config import PARAMS, PATHS
from alpha_agent.notifications import send_whatsapp
from alpha_agent.reporting.signals import Signal, Signals

from .brokers.base import BrokerBase, OptionOrder, Order
from .portfolio import (
    OptionIntent,
    TradeIntent,
    build_option_intents,
    build_target_portfolio,
    check_capital_headroom,
    diff_against_current,
    total_invested_notional,
)

logger = logging.getLogger(__name__)

_DAY_STATE_PATH = PATHS.cache_dir / "trader_day_state.json"


def load_latest_signals() -> Signals:
    path = PATHS.signals_dir / "latest.json"
    if not path.exists():
        raise FileNotFoundError(f"No hay señales en {path}. Corré primero run_analyst.py")
    data = json.loads(path.read_text(encoding="utf-8"))

    sig = Signals(
        generated_at=data["generated_at"],
        horizon=data["horizon"],
        capital_usd=data.get("capital_usd", PARAMS.paper_capital_usd),
        params=data["params"],
        macro=data.get("macro", {}),
        portfolio=data.get("portfolio", {}),
    )
    sig.long_term = [Signal(**s) for s in data.get("long_term", [])]
    sig.short_term = [Signal(**s) for s in data.get("short_term", [])]
    sig.options_book = [Signal(**s) for s in data.get("options_book", [])]
    sig.hedge_book = [Signal(**s) for s in data.get("hedge_book", [])]
    return sig


# ─────────────────────────────────────────────────────────────────────────────
# Kill switch: anchor al equity del inicio del día y checkeo en cada run
# ─────────────────────────────────────────────────────────────────────────────
def _load_day_state() -> dict:
    if _DAY_STATE_PATH.exists():
        try:
            return json.loads(_DAY_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_day_state(state: dict) -> None:
    _DAY_STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _kill_switch_check(broker: BrokerBase) -> tuple[bool, str]:
    """
    Devuelve (ok_to_trade, reason).
    Si el equity de hoy bajó más que PARAMS.max_daily_drawdown respecto al
    anchor del día → False.
    """
    today = date.today().isoformat()
    state = _load_day_state()
    try:
        current_equity = broker.get_equity()
    except Exception as e:
        logger.warning("No pude leer equity: %s — dejo pasar la orden (sin kill switch).", e)
        return True, "equity lookup failed"

    anchor = state.get(today)
    if anchor is None:
        # primer run del día → anclamos
        state = {today: current_equity}
        _save_day_state(state)
        return True, f"anchor intradía seteado en ${current_equity:.2f}"

    drawdown = (anchor - current_equity) / anchor if anchor > 0 else 0.0
    if drawdown > PARAMS.max_daily_drawdown:
        return False, (
            f"KILL SWITCH: drawdown intradía {drawdown*100:.2f}% > "
            f"{PARAMS.max_daily_drawdown*100:.1f}% (anchor ${anchor:.2f} → ahora ${current_equity:.2f})"
        )
    return True, f"drawdown intradía {drawdown*100:+.2f}% dentro del límite"


# ─────────────────────────────────────────────────────────────────────────────
# VIX-adaptive sizing helpers
# ─────────────────────────────────────────────────────────────────────────────
def _get_vix(signals: Signals) -> float:
    try:
        return float(signals.macro.get("prices", {}).get("vix", 18.0))
    except Exception:
        return 18.0


def _vix_multiplier(vix: float) -> float:
    """Reduce capital en alta volatilidad, permite leve agresividad en calma."""
    if vix > 30:
        return 0.60
    if vix > 25:
        return 0.75
    if vix < 15:
        return 1.10
    return 1.0


# ─────────────────────────────────────────────────────────────────────────────
# Execute
# ─────────────────────────────────────────────────────────────────────────────
def execute(broker: BrokerBase, *, dry_run: bool = True, max_capital: float | None = None) -> list[dict]:
    """
    Ejecuta el plan de trading sobre el broker dado.
    """
    if not broker.is_market_open():
        logger.warning("Mercado cerrado. Abortando ejecución.")
        return [{"status": "market_closed"}]

    ok, reason = _kill_switch_check(broker)
    logger.info("Kill switch: %s (%s)", "OK" if ok else "TRIGGERED", reason)
    if not ok:
        send_whatsapp(
            f"🛑 *KILL SWITCH DISPARADO*\n\n{reason}\n\nNo se enviaron órdenes.",
            header="TRADER ALPHA",
        )
        return [{"status": "kill_switch", "reason": reason}]

    signals = load_latest_signals()

    # Capital seguro: usamos min(equity, buying_power) para no operar con margen.
    # Alpaca paper habilita margen 2x → buying_power puede ser 2× equity.
    # Además respetamos max_capital (pasado por run_autonomous.ps1 como el equity
    # real leído antes de correr el analyst, para consistencia).
    equity = broker.get_equity()
    bp = broker.get_buying_power()
    safe_bp = min(equity, bp)   # nunca usar más que el equity real
    capital = min(safe_bp, max_capital) if max_capital else safe_bp
    logger.info(
        "Capital: $%.2f (equity=$%.2f, bp=$%.2f, cap_arg=%s)",
        capital, equity, bp, str(max_capital),
    )

    # ── VIX-adaptive sizing ─────────────────────────────────────────
    vix = _get_vix(signals)
    vix_mult = _vix_multiplier(vix)
    capital_adj = capital * vix_mult
    if vix_mult != 1.0:
        logger.info(
            "VIX %.1f → multiplicador %.0f%% → capital ajustado $%.2f (desde $%.2f)",
            vix, vix_mult * 100, capital_adj, capital,
        )
    else:
        logger.info("VIX %.1f → sizing normal (100%%)", vix)

    fills: list[dict] = [{"status": "meta", "vix": vix, "vix_mult": vix_mult}]

    # ── Equity (LP + CP) ────────────────────────────────────────────
    target = build_target_portfolio(signals, capital_adj)
    positions = broker.get_positions()
    invested = total_invested_notional(positions)
    equity_intents = diff_against_current(target, positions)
    # Guard: no exceder capital disponible (evita sobre-invertir con margin)
    equity_intents = check_capital_headroom(capital, positions, equity_intents)
    logger.info(
        "Equity plan: %d órdenes (capital=$%.2f, ya invertido=$%.2f, headroom=$%.2f)",
        len(equity_intents), capital, invested, capital - invested,
    )
    fills.extend(_submit_equity_intents(broker, equity_intents, dry_run=dry_run))

    # ── Opciones (direccionales + hedge) ────────────────────────────
    option_intents = build_option_intents(signals, capital_adj)
    logger.info("Options plan: %d intents", len(option_intents))
    fills.extend(_submit_option_intents(broker, option_intents, dry_run=dry_run))

    return fills


def _limit_price(price: float, side: str) -> float:
    """
    Precio límite para reducir slippage vs market orders.
    BUY: 0.15% sobre el mid → casi siempre se ejecuta en segundos.
    SELL: 0.15% bajo el mid.
    Esto captura mejor spread en stocks poco líquidos (VIST, YPF, AVAV).
    """
    if side.upper() in ("BUY",):
        return round(price * 1.0015, 2)
    return round(price * 0.9985, 2)


def _submit_equity_intents(broker: BrokerBase, intents: list[TradeIntent], *, dry_run: bool) -> list[dict]:
    fills = []
    for intent in intents:
        try:
            price = broker.get_last_price(intent.ticker)
            qty = round(intent.notional / price, 4) if price > 0 else 0
            if qty <= 0:
                logger.warning("Skip %s: qty = 0", intent.ticker)
                continue

            lp = _limit_price(price, intent.side)
            order = Order(
                ticker=intent.ticker,
                side=intent.side,
                qty=qty,
                order_type="limit",
                limit_price=lp,
                stop_loss=intent.stop_loss,
                take_profit=intent.take_profit,
                client_order_id=f"alpha_eq_{datetime.now().strftime('%Y%m%d%H%M%S')}_{intent.ticker}",
            )

            if dry_run:
                logger.info("[DRY RUN EQUITY] %s @ limit $%.2f (mid $%.2f)", order, lp, price)
                fills.append({
                    "asset": "equity", "ticker": intent.ticker, "side": intent.side,
                    "qty": qty, "status": "dry_run", "order_id": None,
                    "limit_price": lp,
                })
            else:
                oid = broker.submit_order(order)
                fills.append({
                    "asset": "equity", "ticker": intent.ticker, "side": intent.side,
                    "qty": qty, "status": "submitted", "order_id": oid,
                    "limit_price": lp,
                })
        except Exception as e:
            logger.error("Falló equity %s: %s", intent.ticker, e)
            fills.append({
                "asset": "equity", "ticker": intent.ticker, "side": intent.side,
                "qty": 0, "status": f"error: {e}", "order_id": None,
            })
    return fills


def _submit_option_intents(broker: BrokerBase, intents: list[OptionIntent], *, dry_run: bool) -> list[dict]:
    fills = []
    for intent in intents:
        try:
            opt_order = OptionOrder(
                underlying=intent.underlying,
                option_type=intent.option_type,
                target_strike=intent.target_strike,
                target_expiry=intent.target_expiry,
                contracts=intent.contracts,
                side="BUY",
                order_type="market",
                client_order_id=f"alpha_opt_{datetime.now().strftime('%Y%m%d%H%M%S')}_{intent.underlying}",
            )

            if dry_run:
                logger.info(
                    "[DRY RUN OPT] BUY %d x %s %s @ strike %.2f exp %s (est $%.0f)",
                    intent.contracts, intent.underlying, intent.option_type,
                    intent.target_strike, intent.target_expiry, intent.contract_cost_est,
                )
                fills.append({
                    "asset": "option", "underlying": intent.underlying,
                    "type": intent.option_type, "contracts": intent.contracts,
                    "strike": intent.target_strike, "expiry": intent.target_expiry,
                    "role": intent.role, "status": "dry_run", "order_id": None,
                })
            else:
                try:
                    oid = broker.submit_option_order(opt_order)
                    fills.append({
                        "asset": "option", "underlying": intent.underlying,
                        "type": intent.option_type, "contracts": intent.contracts,
                        "strike": intent.target_strike, "expiry": intent.target_expiry,
                        "role": intent.role, "status": "submitted", "order_id": oid,
                    })
                except NotImplementedError as e:
                    logger.warning("Broker no soporta opciones todavía: %s", e)
                    fills.append({
                        "asset": "option", "underlying": intent.underlying,
                        "type": intent.option_type, "contracts": intent.contracts,
                        "status": "unsupported", "order_id": None,
                    })
        except Exception as e:
            logger.error("Falló option %s: %s", intent.underlying, e)
            fills.append({
                "asset": "option", "underlying": intent.underlying,
                "type": intent.option_type, "contracts": intent.contracts,
                "status": f"error: {e}", "order_id": None,
            })
    return fills


def summarize_fills(fills: list[dict]) -> str:
    if not fills:
        return "Sin órdenes ejecutadas."
    meta = next((f for f in fills if f.get("status") == "meta"), {})
    vix = meta.get("vix")
    vix_mult = meta.get("vix_mult")
    real_fills = [f for f in fills if f.get("status") != "meta"]
    header = f"*EJECUTOR* — {len(real_fills)} ordenes/intents"
    if vix is not None and vix_mult is not None and vix_mult != 1.0:
        direction = "reducido" if vix_mult < 1 else "ampliado"
        header += f" | VIX {vix:.0f} -> sizing {direction} al {vix_mult*100:.0f}%"
    lines = [header]
    fills = real_fills
    for f in fills:
        if f.get("status") == "kill_switch":
            lines.append(f"🛑 {f.get('reason')}")
            continue
        if f.get("status") == "market_closed":
            lines.append("⏸ Mercado cerrado, no se operó.")
            continue
        if f.get("asset") == "option":
            lines.append(
                f"• {f.get('type','').upper()} {f.get('underlying')} x{f.get('contracts')} "
                f"strike {f.get('strike')} exp {f.get('expiry')} [{f.get('role')}] — {f.get('status')}"
            )
        else:
            lines.append(
                f"• {f.get('side')} {f.get('qty')} {f.get('ticker')} — {f.get('status')}"
            )
    return "\n".join(lines)


def notify(fills: list[dict]) -> None:
    send_whatsapp(summarize_fills(fills), header="EJECUTOR ALPHA")
