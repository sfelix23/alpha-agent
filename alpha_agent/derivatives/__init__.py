"""
Módulo de derivados: apuestas direccionales con calls/puts + hedge layer.

Diseño:
    - `bearish_scoring`: rankea tickers como candidatos a BUY_PUT (apuesta bajista).
    - `directional_options`: elige strikes/expiries y construye Signals de calls y puts.
    - `hedge_layer`: si el régimen macro está bear o VIX alto, genera puts SPY de cobertura.

Solo long options (L1) por ahora — riesgo máximo = prima pagada, sin margin.
El short_equity está deshabilitado por default (PARAMS.enable_short_equity = False)
porque con capital pequeño un short te come toda la buying power y el loss es ilimitado.
"""

from .bearish import build_bearish_candidates
from .options_builder import build_directional_options_signals, build_hedge_signals

__all__ = [
    "build_bearish_candidates",
    "build_directional_options_signals",
    "build_hedge_signals",
]
