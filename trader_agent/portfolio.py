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

from dataclasses import dataclass

from alpha_agent.config import PARAMS
from alpha_agent.reporting.signals import Signal, Signals

from .brokers.base import Position


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
    cap_lp = capital * PARAMS.weight_long_term
    cap_st = capital * PARAMS.weight_short_term

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
    intents: list[TradeIntent] = []

    # Tickers con sobre-exposición respecto al target → SELL el exceso
    for t, mv in current.items():
        target_notional = target.get(t, {}).get("notional", 0.0)
        delta = target_notional - mv
        if delta < -threshold:
            intents.append(TradeIntent(
                ticker=t, side="SELL", notional=abs(delta),
                horizon=target.get(t, {}).get("horizon", "LP"),
                stop_loss=None, take_profit=None,
            ))

    # Tickers en target que necesitan BUY (delta positivo)
    for t, info in target.items():
        already_invested = current.get(t, 0.0)
        delta = info["notional"] - already_invested
        if delta > threshold:
            intents.append(TradeIntent(
                ticker=t,
                side=info.get("side", "BUY"),
                notional=delta,           # solo la diferencia, nunca el total
                horizon=info["horizon"],
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
) -> list[TradeIntent]:
    """
    Filtra intents de BUY para no exceder el capital disponible.
    Calcula el headroom = capital - lo ya invertido y recorta los BUYs si es necesario.
    """
    invested = total_invested_notional(positions)
    headroom = capital - invested
    if headroom <= 0:
        # Capital totalmente desplegado — solo dejar SELLs
        return [i for i in intents if i.side == "SELL"]

    filtered = []
    buy_total = 0.0
    for intent in intents:
        if intent.side == "SELL":
            filtered.append(intent)
        else:
            if buy_total + intent.notional <= headroom + 50:  # +$50 tolerancia
                filtered.append(intent)
                buy_total += intent.notional
            elif headroom - buy_total > 25:
                # Recortar al headroom disponible
                trimmed = TradeIntent(
                    ticker=intent.ticker,
                    side=intent.side,
                    notional=headroom - buy_total,
                    horizon=intent.horizon,
                    stop_loss=intent.stop_loss,
                    take_profit=intent.take_profit,
                )
                filtered.append(trimmed)
                buy_total = headroom
    return filtered
