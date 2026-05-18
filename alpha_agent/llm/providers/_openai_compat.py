"""Helper base para providers OpenAI-compatible (DeepSeek, OpenRouter).

Ambos exponen el mismo schema que la API de OpenAI, así que reusa el SDK
`openai` cambiando sólo la `base_url`.
"""

from __future__ import annotations

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


class OpenAICompatProvider(BaseProvider):
    """Base para providers que hablan el schema OpenAI Chat Completions.

    Subclase concreta debe definir:
        name, env_var (nombre de la API key), base_url, _model_for(alias),
        cost_per_mtok.
    """

    name = "openai_compat"
    env_var = "OPENAI_API_KEY"
    base_url: str = "https://api.openai.com/v1"
    cost_per_mtok: float = 0.0

    def __init__(self) -> None:
        self._client = None

    def _get_client(self):
        if self._client is not None:
            return self._client
        api_key = os.getenv(self.env_var)
        if not api_key:
            raise ProviderDisabled(f"{self.env_var} no presente")
        try:
            from openai import OpenAI  # type: ignore
        except ImportError as e:
            raise ProviderDisabled(f"openai SDK no instalado: {e}") from e
        self._client = OpenAI(api_key=api_key, base_url=self.base_url)
        return self._client

    def is_available(self) -> bool:
        if not os.getenv(self.env_var):
            return False
        disabled, _ = budget.is_provider_disabled(self.name)
        return not disabled

    def _model_for(self, alias: str) -> str:
        raise NotImplementedError

    def call(self, prompt: str, *, max_tokens: int, model_alias: str = "fast") -> ProviderResult:
        if not self.is_available():
            raise ProviderDisabled(f"{self.name} no disponible")

        client = self._get_client()
        model = self._model_for(model_alias)
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
            raise ProviderError(f"{self.name} call failed: {e}") from e

        latency_ms = int((time.perf_counter() - start) * 1000)
        text = resp.choices[0].message.content if resp.choices else ""
        usage = getattr(resp, "usage", None)
        tokens_in = getattr(usage, "prompt_tokens", 0) or 0
        tokens_out = getattr(usage, "completion_tokens", 0) or 0
        cost = (tokens_in + tokens_out) / 1_000_000 * self.cost_per_mtok
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
