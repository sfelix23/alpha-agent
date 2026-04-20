"""Cálculos financieros: CAPM, Markowitz, técnicos, scoring y Kelly."""
from .capm import compute_capm_metrics
from .markowitz import optimize_portfolio
from .technical import compute_technical_indicators
from .scoring import build_scores
from .kelly import kelly_weights, blend_markowitz_kelly
from .earnings_guard import get_earnings_soon

__all__ = [
    "compute_capm_metrics",
    "optimize_portfolio",
    "compute_technical_indicators",
    "build_scores",
    "kelly_weights",
    "blend_markowitz_kelly",
    "get_earnings_soon",
]
