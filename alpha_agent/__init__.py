"""
alpha_agent — Agente Analista (Agente 1).

Pipeline:
    data  →  analytics  →  scoring  →  reporting  →  notifications

El output principal es un objeto `Signals` (en reporting.signals) que el
trader_agent (Agente 2) consume para decidir órdenes.
"""

__version__ = "0.1.0"
