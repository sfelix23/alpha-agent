"""
trader_agent — Agente Ejecutor (Agente 2).

Lee las señales generadas por alpha_agent y las traduce en órdenes
ejecutables a través de un broker. Soporta múltiples brokers vía la
interfaz BrokerBase. Default: Alpaca paper trading.

Pipeline:
    signals/latest.json  →  Strategy  →  Portfolio  →  BrokerBase  →  fills
"""

__version__ = "0.1.0"
