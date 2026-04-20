"""Contexto macro: precios de commodities, VIX, yields, y detección de régimen."""
from .macro_context import fetch_macro_snapshot, detect_market_regime, MacroSnapshot

__all__ = ["fetch_macro_snapshot", "detect_market_regime", "MacroSnapshot"]
