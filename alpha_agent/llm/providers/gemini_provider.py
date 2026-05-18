"""Provider Google Gemini (Flash 2.0).

Free tier estándar. Buena calidad para sentiment, narrative y clasificación.
Es el provider más estable del free tier — Google rara vez cambia las
políticas sin aviso.
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


class GeminiProvider(BaseProvider):
    name = "gemini"

    def __init__(self) -> None:
        self._client = None

    def _get_client(self):
        if self._client is not None:
            return self._client
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise ProviderDisabled("GOOGLE_API_KEY no presente")
        try:
            from google import genai  # type: ignore
        except ImportError as e:
            raise ProviderDisabled(f"google-genai SDK no instalado: {e}") from e
        self._client = genai.Client(api_key=api_key)
        return self._client

    def is_available(self) -> bool:
        if not os.getenv("GOOGLE_API_KEY"):
            return False
        disabled, _ = budget.is_provider_disabled(self.name)
        return not disabled

    def call(self, prompt: str, *, max_tokens: int, model_alias: str = "default") -> ProviderResult:
        if not self.is_available():
            raise ProviderDisabled("gemini no disponible")

        client = self._get_client()
        model = LLM.gemini_model
        start = time.perf_counter()
        try:
            resp = client.models.generate_content(
                model=model,
                contents=prompt,
                config={"max_output_tokens": max_tokens},
            )
        except Exception as e:
            status = _extract_status_code(e)
            if status in (400, 401, 403):
                reason = f"HTTP {status}: {e}"
                budget.disable_provider(self.name, LLM.disable_provider_on_4xx_hours, reason)
                raise ProviderDisabled(reason) from e
            raise ProviderError(f"gemini call failed: {e}") from e

        latency_ms = int((time.perf_counter() - start) * 1000)
        text = getattr(resp, "text", "") or ""
        usage = getattr(resp, "usage_metadata", None)
        tokens_in = getattr(usage, "prompt_token_count", 0) or 0
        tokens_out = getattr(usage, "candidates_token_count", 0) or 0
        cost = (tokens_in + tokens_out) / 1_000_000 * LLM.cost_per_mtok_gemini
        return ProviderResult(
            text=text,
            provider=self.name,
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=round(cost, 6),
            latency_ms=latency_ms,
        )


def _extract_status_code(exc: Exception) -> int | None:
    for attr in ("status_code", "code", "status"):
        v = getattr(exc, attr, None)
        if isinstance(v, int):
            return v
    return None
