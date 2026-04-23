"""
Implementación de BrokerBase para Alpaca (paper trading por default).

Soporta:
    - Equity market/limit orders (BUY / SELL / SELL_SHORT)
    - Options L1: BUY CALL / BUY PUT (long options only, riesgo limitado a la prima)
    - Option chain fetch: elige el contrato con strike y expiry más cercanos
      al pedido del alpha_agent

Credenciales en .env:
    ALPACA_API_KEY
    ALPACA_SECRET_KEY

IMPORTANTE: paper=True por default por seguridad. Para real money hay que
pasarlo explícitamente desde run_trader.py.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

from .base import BrokerBase, OptionOrder, Order, Position

logger = logging.getLogger(__name__)


class AlpacaBroker(BrokerBase):
    def __init__(self, *, paper: bool = True):
        self.paper = paper
        api_key = os.getenv("ALPACA_API_KEY")
        secret = os.getenv("ALPACA_SECRET_KEY")
        if not (api_key and secret):
            raise RuntimeError("Faltan ALPACA_API_KEY / ALPACA_SECRET_KEY en .env")

        try:
            from alpaca.trading.client import TradingClient  # type: ignore
            from alpaca.data.historical import StockHistoricalDataClient  # type: ignore
        except ImportError as e:
            raise RuntimeError("Falta alpaca-py: `pip install alpaca-py`") from e

        self._trading = TradingClient(api_key, secret, paper=paper)
        self._data = StockHistoricalDataClient(api_key, secret)
        self._api_key = api_key
        self._secret = secret
        logger.info("Alpaca conectado (paper=%s)", paper)

    # ──────────────────────────────────────────────────────────────────
    # Account
    # ──────────────────────────────────────────────────────────────────
    def get_buying_power(self) -> float:
        acc = self._trading.get_account()
        return float(acc.buying_power)

    def get_equity(self) -> float:
        acc = self._trading.get_account()
        return float(acc.equity)

    def get_positions(self) -> list[Position]:
        out = []
        for p in self._trading.get_all_positions():
            asset_class = "option" if getattr(p, "asset_class", "") == "us_option" else "equity"
            out.append(Position(
                ticker=p.symbol,
                qty=float(p.qty),
                avg_price=float(p.avg_entry_price),
                market_value=float(p.market_value),
                unrealized_pl=float(p.unrealized_pl),
                asset_class=asset_class,
            ))
        return out

    def get_last_price(self, ticker: str) -> float:
        from alpaca.data.requests import StockLatestQuoteRequest  # type: ignore
        req = StockLatestQuoteRequest(symbol_or_symbols=ticker)
        quote = self._data.get_stock_latest_quote(req)
        q = quote[ticker]
        return float((q.ask_price + q.bid_price) / 2)

    # ──────────────────────────────────────────────────────────────────
    # Equity orders
    # ──────────────────────────────────────────────────────────────────
    def submit_order(self, order: Order) -> str:
        from alpaca.trading.enums import OrderSide, TimeInForce  # type: ignore
        from alpaca.trading.requests import (  # type: ignore
            LimitOrderRequest,
            MarketOrderRequest,
        )

        side_map = {
            "BUY": OrderSide.BUY,
            "SELL": OrderSide.SELL,
            "SELL_SHORT": OrderSide.SELL,   # short = sell sin posición previa
        }
        side = side_map.get(order.side.upper(), OrderSide.BUY)

        common = dict(
            symbol=order.ticker,
            qty=order.qty,
            side=side,
            time_in_force=TimeInForce.DAY,
            client_order_id=order.client_order_id,
        )
        if order.order_type == "limit" and order.limit_price:
            req = LimitOrderRequest(limit_price=order.limit_price, **common)
        else:
            req = MarketOrderRequest(**common)

        result = self._trading.submit_order(req)
        logger.info(
            "Orden equity: %s %s %s → id=%s",
            order.side, order.qty, order.ticker, result.id,
        )
        return str(result.id)

    # ──────────────────────────────────────────────────────────────────
    # Options (L1: long calls + long puts)
    # ──────────────────────────────────────────────────────────────────
    def get_option_chain(self, underlying: str, expiry: str | None = None) -> list[dict]:
        """
        Devuelve lista de contratos disponibles. Usa GetOptionContractsRequest.
        """
        try:
            from alpaca.trading.requests import GetOptionContractsRequest  # type: ignore
            from alpaca.trading.enums import AssetStatus  # type: ignore
        except ImportError:
            raise RuntimeError("Versión de alpaca-py sin soporte de opciones. Actualizá: pip install -U alpaca-py")

        req = GetOptionContractsRequest(
            underlying_symbols=[underlying],
            status=AssetStatus.ACTIVE,
            expiration_date=expiry,
        )
        resp = self._trading.get_option_contracts(req)
        contracts = getattr(resp, "option_contracts", []) or []
        out = []
        for c in contracts:
            out.append({
                "symbol": c.symbol,
                "underlying": c.underlying_symbol,
                "strike": float(c.strike_price),
                "expiry": str(c.expiration_date),
                "option_type": str(c.type).lower().replace("optiontype.", ""),
                "open_interest": getattr(c, "open_interest", 0),
            })
        return out

    def _find_closest_contract(
        self,
        underlying: str,
        target_strike: float,
        target_expiry: str,
        option_type: str,
    ) -> dict | None:
        """
        Busca el contrato más cercano en strike + expiry al pedido.
        """
        chain = self.get_option_chain(underlying)
        if not chain:
            return None
        # Filtrar por tipo
        chain = [c for c in chain if c["option_type"].endswith(option_type.lower())]
        if not chain:
            return None

        target_date = datetime.fromisoformat(target_expiry).date()

        def _score(c: dict) -> tuple[int, float]:
            try:
                d = datetime.fromisoformat(c["expiry"]).date()
                days_diff = abs((d - target_date).days)
            except Exception:
                days_diff = 999
            strike_diff = abs(c["strike"] - target_strike)
            return (days_diff, strike_diff)

        chain.sort(key=_score)
        return chain[0]

    def submit_option_order(self, order: OptionOrder) -> str:
        """
        Envía una orden de opción. Por ahora solo L1 (BUY de call/put).
        """
        if order.side.upper() != "BUY":
            raise NotImplementedError("Solo long options (BUY) soportado por ahora.")

        contract = self._find_closest_contract(
            underlying=order.underlying,
            target_strike=order.target_strike,
            target_expiry=order.target_expiry,
            option_type=order.option_type,
        )
        if contract is None:
            raise RuntimeError(
                f"No se encontró contrato para {order.underlying} "
                f"{order.option_type} strike~{order.target_strike} exp~{order.target_expiry}"
            )

        symbol = contract["symbol"]
        logger.info(
            "Option chain match: %s (strike %.2f, exp %s) para pedido strike~%.2f exp~%s",
            symbol, contract["strike"], contract["expiry"],
            order.target_strike, order.target_expiry,
        )

        from alpaca.trading.enums import OrderSide, TimeInForce  # type: ignore
        from alpaca.trading.requests import (  # type: ignore
            LimitOrderRequest,
            MarketOrderRequest,
        )

        common = dict(
            symbol=symbol,
            qty=order.contracts,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            client_order_id=order.client_order_id,
        )
        if order.order_type == "limit" and order.limit_price:
            req = LimitOrderRequest(limit_price=order.limit_price, **common)
        else:
            req = MarketOrderRequest(**common)

        result = self._trading.submit_order(req)
        logger.info(
            "Orden opción: BUY %d x %s → id=%s",
            order.contracts, symbol, result.id,
        )
        return str(result.id)

    # ──────────────────────────────────────────────────────────────────
    def cancel_all(self) -> None:
        self._trading.cancel_orders()

    def is_market_open(self) -> bool:
        clock = self._trading.get_clock()
        return bool(clock.is_open)

    # ──────────────────────────────────────────────────────────────────
    # Trailing stop management
    # ──────────────────────────────────────────────────────────────────
    def get_open_orders(self, ticker: str) -> list:
        """Devuelve órdenes abiertas para un ticker."""
        try:
            from alpaca.trading.requests import GetOrdersRequest  # type: ignore
            from alpaca.trading.enums import QueryOrderStatus     # type: ignore
            req = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[ticker])
            return self._trading.get_orders(req) or []
        except Exception as e:
            logger.debug("get_open_orders(%s): %s", ticker, e)
            return []

    def update_stop_loss(self, ticker: str, new_stop: float, qty: float) -> str | None:
        """
        Cancela órdenes stop abiertas para el ticker y envía una nueva stop-sell.
        Devuelve el order_id de la nueva orden, o None si hubo error.
        """
        from alpaca.trading.enums import OrderSide, TimeInForce  # type: ignore
        from alpaca.trading.requests import StopOrderRequest      # type: ignore

        # Cancelar stops existentes
        for o in self.get_open_orders(ticker):
            o_type = str(getattr(o, "order_type", "")).lower()
            if "stop" in o_type:
                try:
                    self._trading.cancel_order_by_id(str(o.id))
                    logger.info("Stop cancelado para %s (id=%s)", ticker, o.id)
                except Exception as e:
                    logger.warning("No pude cancelar stop %s: %s", o.id, e)

        # Enviar nuevo stop-sell
        # Alpaca requiere TimeInForce.DAY para cantidades fraccionales (error 42210000 con GTC)
        try:
            is_fractional = (round(abs(qty), 4) % 1 != 0)
            tif = TimeInForce.DAY if is_fractional else TimeInForce.GTC

            req = StopOrderRequest(
                symbol=ticker,
                qty=round(abs(qty), 4),
                side=OrderSide.SELL,
                time_in_force=tif,
                stop_price=round(new_stop, 2),
            )
            result = self._trading.submit_order(req)
            logger.info(
                "Trailing stop actualizado: SELL %s @ stop $%.2f (tif=%s) → id=%s",
                ticker, new_stop, tif.value, result.id,
            )
            return str(result.id)
        except Exception as e:
            logger.error(
                "Stop order FALLIDA para %s @ $%.2f: %s — revisar en Alpaca paper dashboard",
                ticker, new_stop, e,
            )
            return None
