"""Provider DeepSeek (chat + reasoner R1).

API OpenAI-compatible. Free tier hasta cierta cuota; después cobra muy barato.
R1 reasoning tiene calidad similar a Sonnet para tesis profundas.
"""

from __future__ import annotations

from alpha_agent.config import LLM
from alpha_agent.llm.providers._openai_compat import OpenAICompatProvider


class DeepSeekProvider(OpenAICompatProvider):
    name = "deepseek"
    env_var = "DEEPSEEK_API_KEY"
    base_url = "https://api.deepseek.com/v1"

    @property
    def cost_per_mtok(self) -> float:
        return LLM.cost_per_mtok_deepseek

    def _model_for(self, alias: str) -> str:
        if alias in ("reasoning", "deep"):
            return LLM.deepseek_reasoning_model
        return LLM.deepseek_chat_model
