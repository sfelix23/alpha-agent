"""Provider Groq (Llama 3.3 70B + DeepSeek R1 distill).

Free tier muy generoso (~14K requests/día). Ultra rápido (10x más rápido que
Claude por token). Default primario del gateway en la cascada.

Modelos:
- fast       → llama-3.3-70b-versatile (sentiment, narrative, decisión rápida)
- reasoning  → deepseek-r1-distill-llama-70b (wall street, risk debate)
"""

from __future__ import annotations

import logging
import os
import time

from alpha_agent.config import LLM
from alpha_agent.llm import budget
from alpha_agent.llm.providers.base import (
    BaseProvider,
    ProviderDisabled,
    ProviderError,
    ProviderResult,
)

logger = logging.getLogger(__name__)


class GroqProvider(BaseProvider):
    name = "groq"

    def __init__(self) -> None:
        self._client = None

    def _get_client(self):
        if self._client is not None:
            return self._client
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise ProviderDisabled("GROQ_API_KEY no presente")
        try:
            from groq import Groq  # type: ignore
        except ImportError as e:
            raise ProviderDisabled(f"groq SDK no instalado: {e}") from e
        self._client = Groq(api_key=api_key)
        return self._client

    def is_available(self) -> bool:
        if not os.getenv("GROQ_API_KEY"):
            return False
        disabled, _ = budget.is_provider_disabled(self.name)
        return not disabled

    def _resolve_model(self, model_alias: str) -> str:
        if model_alias in ("reasoning", "deep"):
            return LLM.groq_reasoning_model
        return LLM.groq_fast_model

    def call(self, prompt: str, *, max_tokens: int, model_alias: str = "fast") -> ProviderResult:
        if not self.is_available():
            raise ProviderDisabled("groq no disponible")

        client = self._get_client()
        model = self._resolve_model(model_alias)
        start = time.perf_counter()
        try:
            resp = client.chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:
            status = _extract_status_code(e)
            if status in (400, 401, 403):
                reason = f"HTTP {status}: {e}"
                budget.disable_provider(self.name, LLM.disable_provider_on_4xx_hours, reason)
                raise ProviderDisabled(reason) from e
            raise ProviderError(f"groq call failed: {e}") from e

        latency_ms = int((time.perf_counter() - start) * 1000)
        text = resp.choices[0].message.content if resp.choices else ""
        usage = getattr(resp, "usage", None)
        tokens_in = getattr(usage, "prompt_tokens", 0) or 0
        tokens_out = getattr(usage, "completion_tokens", 0) or 0
        cost = (tokens_in + tokens_out) / 1_000_000 * LLM.cost_per_mtok_groq
        return ProviderResult(
            text=text or "",
            provider=self.name,
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=round(cost, 6),
            latency_ms=latency_ms,
        )


def _extract_status_code(exc: Exception) -> int | None:
    for attr in ("status_code", "status", "code"):
        v = getattr(exc, attr, None)
        if isinstance(v, int):
            return v
    resp = getattr(exc, "response", None)
    if resp is not None:
        v = getattr(resp, "status_code", None)
        if isinstance(v, int):
            return v
    return None
