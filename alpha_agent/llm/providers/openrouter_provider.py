"""Provider OpenRouter (gateway a cientos de modelos gratis).

API OpenAI-compatible apuntando a openrouter.ai. Usa modelos con sufijo
`:free` para no consumir presupuesto. Útil como último fallback antes de
la heurística determinista.
"""

from __future__ import annotations

from alpha_agent.config import LLM
from alpha_agent.llm.providers._openai_compat import OpenAICompatProvider


class OpenRouterProvider(OpenAICompatProvider):
    name = "openrouter"
    env_var = "OPENROUTER_API_KEY"
    base_url = "https://openrouter.ai/api/v1"

    @property
    def cost_per_mtok(self) -> float:
        return LLM.cost_per_mtok_openrouter

    def _model_for(self, alias: str) -> str:
        if alias in ("reasoning", "deep"):
            return LLM.openrouter_reasoning_model
        return LLM.openrouter_fast_model
