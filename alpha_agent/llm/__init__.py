"""LLM gateway multi-provider con budget, cache, rate limit y fallback.

Punto de entrada único para todas las llamadas a modelos de lenguaje en el
sistema. Reemplaza el patrón de `anthropic.Anthropic()` y `genai.Client()`
disperso en `claude_analyst.py` y `sentiment.py`.

Uso típico:

    from alpha_agent.llm import call_llm

    result = call_llm(
        "Clasificá este headline...",
        purpose="event_score",
        max_tokens=10,
    )

El gateway elige el provider, cachea, trackea presupuesto y devuelve el texto
(o None si todos los providers fallaron y no hay cache).
"""

from alpha_agent.llm.gateway import call_llm, get_gateway_status

__all__ = ["call_llm", "get_gateway_status"]
