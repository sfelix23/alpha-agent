"""Generación de señales estructuradas y reportes ejecutivos."""
from .signals import Signal, Signals, build_signals
from .ai_report import generate_executive_report

__all__ = ["Signal", "Signals", "build_signals", "generate_executive_report"]
