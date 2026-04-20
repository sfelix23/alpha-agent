"""
Capa de razonamiento: convierte números sueltos (CAPM, técnicos, news, macro)
en una TESIS FINANCIERA explícita por cada trade.

Cada `TradeThesis` responde a:
    - ¿Qué dice la teoría (CAPM, Markowitz)?
    - ¿Qué dice el precio (momentum, RSI, ATR)?
    - ¿Qué dice la narrativa (news sentiment)?
    - ¿Qué dice el entorno (régimen, commodities)?
    - ¿Cuánto puedo perder si me equivoco?
"""
from .trade_reasoning import TradeThesis, build_trade_thesis

__all__ = ["TradeThesis", "build_trade_thesis"]
