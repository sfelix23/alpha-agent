"""Interfaces y tipos compartidos por todos los providers LLM."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class ProviderError(Exception):
    """Error genérico de provider. El gateway atrapa y cae al siguiente."""


class ProviderDisabled(ProviderError):
    """Provider deshabilitado (flag OFF, key faltante, o auto-disable por 4xx).

    Cuando se levanta, el gateway pasa al siguiente provider sin contar como
    intento de retry — porque desactivar no es un fallo transitorio.
    """


class RateLimitExceeded(ProviderError):
    """El rate limiter local bloqueó la llamada. El gateway pasa al siguiente."""


@dataclass(frozen=True)
class ProviderResult:
    """Resultado normalizado de una llamada LLM.

    Todos los providers devuelven un ProviderResult con la misma forma para
    que el gateway pueda comparar costos, tokens y cache hits sin importar
    qué backend respondió.
    """

    text: str
    provider: str               # "anthropic" | "groq" | "gemini" | "deepseek" | "openrouter"
    model: str                  # nombre exacto del modelo invocado
    tokens_in: int              # tokens del prompt (estimado si el provider no lo expone)
    tokens_out: int             # tokens de la respuesta
    cost_usd: float             # costo estimado de esta llamada
    latency_ms: int             # latencia de la llamada


class BaseProvider(ABC):
    """Contrato que todos los providers cumplen.

    Cada provider concreto sabe: cómo armar el cliente (lazy), cómo invocar
    el modelo, cómo extraer tokens y latencia, y cómo calcular costo USD
    según el pricing config en LLMConfig.
    """

    #: Identificador corto del provider, usado en logs, budget JSON y cascada.
    name: str = "base"

    @abstractmethod
    def is_available(self) -> bool:
        """¿El provider está listo para recibir una llamada?

        Considera: flag enable_*, API key presente, no auto-disabled por 4xx.
        El gateway lo llama antes de invocar para fallar rápido y pasar al
        siguiente provider en la cascada.
        """

    @abstractmethod
    def call(
        self,
        prompt: str,
        *,
        max_tokens: int,
        model_alias: str = "fast",
    ) -> ProviderResult:
        """Invoca al modelo.

        Args:
            prompt: texto a enviar.
            max_tokens: límite de tokens de salida.
            model_alias: "fast" | "deep" | "reasoning" | "default" — el provider
                lo resuelve contra su config (ej. Anthropic "fast" → Haiku,
                "deep" → Sonnet).

        Raises:
            ProviderDisabled: provider no disponible (flag OFF, key faltante, auto-disable).
            RateLimitExceeded: rate limiter local rechazó.
            ProviderError: error de red, 4xx, 5xx, parsing.
        """

    def disable_until(self, hours: float, reason: str) -> None:
        """Auto-disable temporal (escrito por el gateway tras 400/401/403).

        Implementación default es no-op; los providers concretos persisten el
        estado a `signals/llm_provider_state.json` vía el módulo `budget`.
        """
