"""
Radar de mercado: escaneo noticioso + movimiento de precio del universo
completo (no sólo los tickers elegidos por el agente). Permite que el
usuario vea qué está pasando con TODOS los activos que tiene en watchlist,
no únicamente los picks del día.
"""

from .universe_radar import MarketRadar, RadarEntry, build_market_radar

__all__ = ["MarketRadar", "RadarEntry", "build_market_radar"]
