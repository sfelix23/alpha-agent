"""
Interfaz abstracta para brokers.

Cualquier broker (Alpaca, Cocos, IBKR…) debe implementar estos métodos. Esto
permite cambiar de venue cambiando una sola línea en run_trader.py.

Soporta:
    - Equity long/short (cash)
    - Opciones long (buy call / buy put)

Los métodos de opciones son opcionales: si un broker no las soporta, lanzará
NotImplementedError y el trader_agent loguea y saltea esas señales.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Order:
    ticker: str
    side: str             # "BUY" | "SELL" | "SELL_SHORT"
    qty: float
    order_type: str = "market"   # "market" | "limit"
    limit_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    client_order_id: str | None = None


@dataclass
class OptionOrder:
    """
    Orden de opción. Se resuelve contra la chain real del broker al momento
    de ejecutar (se elige el contrato más cercano a strike+expiry pedidos).
    """
    underlying: str            # ticker del subyacente (ej "SPY")
    option_type: str           # "call" | "put"
    target_strike: float       # strike pedido (el broker elegirá el más cercano)
    target_expiry: str         # YYYY-MM-DD (el broker elegirá el más cercano)
    contracts: int             # 1 contrato = 100 shares
    side: str = "BUY"          # por ahora solo long options (L1)
    order_type: str = "market"
    limit_price: float | None = None
    client_order_id: str | None = None


@dataclass
class Position:
    ticker: str
    qty: float
    avg_price: float
    market_value: float
    unrealized_pl: float
    asset_class: str = "equity"   # "equity" | "option"


class BrokerBase(ABC):
    """Contrato mínimo que todo broker debe cumplir."""

    @abstractmethod
    def get_buying_power(self) -> float: ...

    @abstractmethod
    def get_equity(self) -> float:
        """Equity total de la cuenta (cash + market value de todas las posiciones)."""

    @abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abstractmethod
    def get_last_price(self, ticker: str) -> float: ...

    @abstractmethod
    def submit_order(self, order: Order) -> str:
        """Devuelve el id de la orden enviada."""

    def submit_option_order(self, order: OptionOrder) -> str:
        """
        Envía una orden de opción. Método opcional — default = NotImplementedError.
        """
        raise NotImplementedError(
            f"{type(self).__name__} no soporta órdenes de opciones todavía."
        )

    def get_option_chain(self, underlying: str, expiry: str | None = None) -> list[dict]:
        """
        Devuelve la chain de opciones disponibles. Opcional.
        Formato: [{symbol, strike, expiry, option_type, bid, ask, open_interest}, ...]
        """
        raise NotImplementedError(
            f"{type(self).__name__} no expone option chain todavía."
        )

    @abstractmethod
    def cancel_all(self) -> None: ...

    @abstractmethod
    def is_market_open(self) -> bool: ...
