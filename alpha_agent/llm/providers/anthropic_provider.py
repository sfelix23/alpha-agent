"""Provider Anthropic Claude (Haiku 4.5 / Sonnet 4.6).

Default OFF — requiere `LLM.enable_anthropic=True` y créditos cargados en la
cuenta. Sonnet detrás del flag adicional `LLM.enable_sonnet=True`.

Sobre el incidente del 400 "empresa deshabilitada": este provider trata los
4xx como errores estructurales (auth, billing, abuse) y NO los reintenta —
desactiva el provider 24h y notifica al gateway. Cualquier retry agresivo
ante 400 amplificaría el flag.
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


class AnthropicProvider(BaseProvider):
    name = "anthropic"

    def __init__(self) -> None:
        self._client = None  # lazy init

    def _get_client(self):
        if self._client is not None:
            return self._client
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ProviderDisabled("ANTHROPIC_API_KEY no presente")
        try:
            import anthropic  # type: ignore
        except ImportError as e:
            raise ProviderDisabled(f"anthropic SDK no instalado: {e}") from e
        self._client = anthropic.Anthropic(api_key=api_key)
        return self._client

    def is_available(self) -> bool:
        if not LLM.enable_anthropic:
            return False
        if not os.getenv("ANTHROPIC_API_KEY"):
            return False
        disabled, _ = budget.is_provider_disabled(self.name)
        if disabled:
            return False
        if budget.is_budget_exhausted(self.name):
            return False
        return True

    def _resolve_model(self, model_alias: str) -> str:
        if model_alias == "deep":
            if not LLM.enable_sonnet:
                raise ProviderDisabled("sonnet flag OFF — usar enable_sonnet=True para activar")
            return LLM.anthropic_deep_model
        return LLM.anthropic_fast_model

    def call(self, prompt: str, *, max_tokens: int, model_alias: str = "fast") -> ProviderResult:
        if not self.is_available():
            raise ProviderDisabled("anthropic no disponible")

        client = self._get_client()
        model = self._resolve_model(model_alias)
        start = time.perf_counter()
        try:
            msg = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:  # SDK raises BadRequestError, AuthenticationError, etc.
            status = _extract_status_code(e)
            if status in (400, 401, 403):
                # Auto-disable 24h, no retry. Esto es lo que evita el abuse-flag.
                reason = f"HTTP {status}: {e}"
                budget.disable_provider(self.name, LLM.disable_provider_on_4xx_hours, reason)
                # Notificar vía WhatsApp se hace en el gateway (acá sólo levantamos).
                raise ProviderDisabled(reason) from e
            raise ProviderError(f"anthropic call failed: {e}") from e

        latency_ms = int((time.perf_counter() - start) * 1000)
        text = msg.content[0].text if msg.content else ""
        usage = getattr(msg, "usage", None)
        tokens_in = getattr(usage, "input_tokens", 0) or 0
        tokens_out = getattr(usage, "output_tokens", 0) or 0
        rate = (
            LLM.cost_per_mtok_anthropic_sonnet
            if model_alias == "deep"
            else LLM.cost_per_mtok_anthropic_haiku
        )
        cost = (tokens_in + tokens_out) / 1_000_000 * rate
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
    """Best-effort extraction de status code de un error del SDK Anthropic."""
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
