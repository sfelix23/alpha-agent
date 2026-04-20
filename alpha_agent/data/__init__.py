"""Capa de datos: descarga y cacheo de precios históricos."""
from .market_data import download_universe, load_benchmark
__all__ = ["download_universe", "load_benchmark"]
