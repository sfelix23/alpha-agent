"""
Construcción de cartera target a partir de las señales.

Soporta tres buckets:
    - LP (equity long)  → PARAMS.weight_long_term
    - CP (equity long)  → PARAMS.weight_short_term
    - Options / hedge   → PARAMS.weight_options

Las señales equity se expresan como TradeIntent (notional USD).
Las señales de opciones se expresan como OptionIntent (contracts a comprar).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from alpha_agent.config import PARAMS
from alpha_agent.reporting.signals import Signal, Signals

from .brokers.base import Position

logger = logging.getLogger(__name__)


@dataclass
class TradeIntent:
    ticker: str
    side: str                   # "BUY" | "SELL" | "SELL_SHORT"
    notional: float
    horizon: str                # "LP" | "CP"
    stop_loss: float | None
    take_profit: float | None


@dataclass
class OptionIntent:
    underlying: str
    option_type: str            # "call" | "put"
    target_strike: float
    target_expiry: str
    contracts: int
    premium_per_share_est: float
    contract_cost_est: float
    horizon: str                # "DERIV" | "HEDGE"
    role: str = "directional"   # "directional" | "hedge"


def build_target_portfolio(signals: Signals, capital: float) -> dict[str, dict]:
    """
    Devuelve {ticker: {notional, horizon, stop_loss, take_profit, side}}
    solo para la parte equity (LP + CP).
    """
    target: dict[str, dict] = {}
    _sp = (signals.params or {}) if hasattr(signals, "params") else {}
    cap_lp = capital * _sp.get("weight_long_term", PARAMS.weight_long_term)
    cap_st = capital * _sp.get("weight_short_term", PARAMS.weight_short_term)

    for s in signals.long_term:
        target[s.ticker] = {
            "notional": cap_lp * s.weight_target,
            "horizon": "LP",
            "side": s.side,      # casi siempre "BUY"
            "stop_loss": s.stop_loss,
            "take_profit": s.take_profit,
        }
    for s in signals.short_term:
        prev = target.get(s.ticker, {"notional": 0, "horizon": "MIX", "side": "BUY",
                                      "stop_loss": None, "take_profit": None})
        target[s.ticker] = {
            "notional": prev["notional"] + cap_st * s.weight_target,
            "horizon": "MIX" if prev["notional"] > 0 else "CP",
            "side": s.side,
            "stop_loss": s.stop_loss or prev.get("stop_loss"),
            "take_profit": s.take_profit or prev.get("take_profit"),
        }
    return target


def build_option_intents(signals: Signals, capital: float) -> list[OptionIntent]:
    """
    Convierte las señales del bucket opciones (options_book + hedge_book) en
    OptionIntents concretos. Calcula el número de contratos respetando el
    presupuesto del sleeve y el cap por contrato.
    """
    if not PARAMS.enable_options:
        return []

    cap_opts = capital * PARAMS.weight_options
    intents: list[OptionIntent] = []
    spent = 0.0

    def _from_signal(s: Signal, role: str) -> OptionIntent | None:
        nonlocal spent
        if s.option is None:
            return None
        budget_for_this = cap_opts * s.weight_target
        contract_cost = s.option.get("contract_cost_est", 0.0)
        if contract_cost <= 0:
            return None
        contracts = max(1, int(budget_for_this // contract_cost))
        contracts = min(contracts, PARAMS.max_contracts_per_trade)
        # Respetar el cap total del sleeve
        planned_cost = contracts * contract_cost
        if spent + planned_cost > cap_opts:
            contracts = max(0, int((cap_opts - spent) // contract_cost))
        if contracts <= 0:
            return None
        spent += contracts * contract_cost

        return OptionIntent(
            underlying=s.ticker,
            option_type=s.option["type"],
            target_strike=s.option["strike"],
            target_expiry=s.option["expiry"],
            contracts=contracts,
            premium_per_share_est=s.option["premium_per_share_est"],
            contract_cost_est=contract_cost,
            horizon=s.horizon,
            role=role,
        )

    # HEDGE primero (protección de capital es prioridad uno en bear regime),
    # después direccionales con el presupuesto que quede en el sleeve.
    for s in signals.hedge_book:
        oi = _from_signal(s, role="hedge")
        if oi:
            intents.append(oi)
    for s in signals.options_book:
        oi = _from_signal(s, role="directional")
        if oi:
            intents.append(oi)

    return intents


def diff_against_current(
    target: dict[str, dict],
    positions: list[Position],
    *,
    threshold: float = 25.0,
) -> list[TradeIntent]:
    """
    Compara target vs posiciones actuales de equity y devuelve órdenes a enviar.
    Ignora posiciones de tipo option (esas se manejan aparte por OptionIntent).

    Guard de capital: el notional de cada BUY está limitado al target minus
    lo que ya está invertido. Nunca envía más de lo que el target permite.
    """
    current = {
        p.ticker: p.market_value
        for p in positions
        if getattr(p, "asset_class", "equity") == "equity"
    }
    # iter18: costo base por ticker para proteger ganadores en la rotación.
    _cost = {
        p.ticker: (getattr(p, "avg_price", 0) or 0) * (getattr(p, "qty", 0) or 0)
        for p in positions
        if getattr(p, "asset_class", "equity") == "equity"
    }
    # iter24: NO rotar a CASH posiciones que no son perdedoras. En una cuenta cash,
    # vender por rotación deja el producido SIN LIQUIDAR (T+1) → no se puede recomprar
    # el mismo día → cash drag (hoy 33% desplegado tras vender VIST+MU ~flat). Momentum
    # sano = cortar perdedores, mantener el resto. Solo rotamos a cash si pnl < -3%
    # (perdedor real); el monitor maneja stop/TP/trailing/max-hold de los demás.
    _ROTATE_LOSER_PCT = -3.0
    intents: list[TradeIntent] = []

    # Tickers con sobre-exposición respecto al target → SELL el exceso
    for t, mv in current.items():
        target_notional = target.get(t, {}).get("notional", 0.0)
        delta = target_notional - mv
        if delta < -threshold:
            cost = _cost.get(t, 0.0)
            pnl_pct = ((mv - cost) / cost * 100) if cost > 0 else 0.0
            # Solo rotación a cash (target=0): mantener si NO es perdedor (≥ -3%).
            if target_notional <= 0 and pnl_pct >= _ROTATE_LOSER_PCT:
                logger.info(
                    "Hold protection: %s %.1f%% fuera de target pero no perdedor — NO rotar a cash",
                    t, pnl_pct,
                )
                continue
            intents.append(TradeIntent(
                ticker=t, side="SELL", notional=abs(delta),
                horizon=target.get(t, {}).get("horizon", "LP"),
                stop_loss=None, take_profit=None,
            ))

    # Guard: tickers ya comprados hoy → no comprar de nuevo
    _bought_today: set[str] = set()
    try:
        from alpha_agent.analytics.trade_db import get_trades
        from datetime import date
        _today = date.today().isoformat()
        for _t in get_trades(limit=50):
            if _t.get("side") == "BUY" and (_t.get("date") or "")[:10] == _today:
                _bought_today.add(_t.get("ticker", ""))
    except Exception:
        pass

    # Guard LP: no abrir segunda posición en un ticker que ya está en cartera LP
    _lp_held: set[str] = set()
    for p in positions:
        if getattr(p, "asset_class", "equity") == "equity":
            _lp_held.add(p.ticker)

    # Tickers en target que necesitan BUY (delta positivo)
    MIN_NOTIONAL = 150.0  # posiciones menores a $150 no mueven la aguja
    for t, info in target.items():
        if t in _bought_today:
            continue  # ya compramos hoy, no duplicar
        horizon = info["horizon"]
        # LP: si ya hay posición abierta en este ticker, no acumular
        if horizon == "LP" and t in _lp_held:
            continue
        already_invested = current.get(t, 0.0)
        delta = info["notional"] - already_invested
        if delta > max(threshold, MIN_NOTIONAL):
            intents.append(TradeIntent(
                ticker=t,
                side=info.get("side", "BUY"),
                notional=delta,
                horizon=horizon,
                stop_loss=info.get("stop_loss"),
                take_profit=info.get("take_profit"),
            ))

    return intents


def total_invested_notional(positions: list[Position]) -> float:
    """Suma el market_value de todas las posiciones equity abiertas."""
    return sum(
        p.market_value for p in positions
        if getattr(p, "asset_class", "equity") == "equity"
    )


def check_capital_headroom(
    capital: float,
    positions: list[Position],
    intents: list[TradeIntent],
    buying_power: float | None = None,
) -> list[TradeIntent]:
    """
    Filtra intents de BUY para no exceder el capital disponible.
    Acredita el producido de los SELLs pendientes al headroom para que
    la rotación (sell viejo → buy nuevo) use todo el capital disponible.

    iter19 fix del cash drag: el headroom correcto = min(buying_power, equity - invested).
    Antes se pasaba capital=min(equity, bp) y se restaba invested OTRA VEZ → doble
    resta (bp ya excluye lo invertido) → desplegaba ~$219 cuando había ~$970 libres.
    Ahora `capital` debe ser EQUITY; `buying_power` capea al cash real del broker.
    Si buying_power es None (callers viejos/tests), mantiene el comportamiento previo.
    """
    invested = total_invested_notional(positions)
    # Los SELLs del mismo ciclo liberan capital: los acreditamos al headroom.
    # Esto permite rotación completa: cierra PBR → abre TSM sin esperar T+1.
    pending_sell_proceeds = sum(
        i.notional for i in intents if i.side == "SELL"
    )
    # Tope sin margen: nunca exponer más que el equity (equity - invested = cash libre).
    headroom = capital - invested + pending_sell_proceeds
    # Tope duro por buying power real del broker (cash liquidado disponible HOY).
    if buying_power is not None:
        headroom = min(headroom, buying_power + pending_sell_proceeds)
    headroom = max(0.0, headroom)

    filtered = []
    buy_total = 0.0
    for intent in intents:
        if intent.side == "SELL":
            filtered.append(intent)
            continue

        available = headroom - buy_total
        if available <= 25:
            break  # sin headroom restante

        if intent.notional <= available + 50:  # +$50 tolerancia de redondeo
            filtered.append(intent)
            buy_total += intent.notional
        else:
            # Recortar al headroom disponible si queda algo significativo
            trimmed = TradeIntent(
                ticker=intent.ticker,
                side=intent.side,
                notional=available,
                horizon=intent.horizon,
                stop_loss=intent.stop_loss,
                take_profit=intent.take_profit,
            )
            filtered.append(trimmed)
            buy_total = headroom

    return filtered
