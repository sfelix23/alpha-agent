"""Rate limiter in-process por provider (token bucket simple).

El estado vive en memoria del proceso. Cloud Run Jobs son efímeros (cada Job
arranca de cero), así que no necesitamos persistir — el budget tracker
maneja los límites diarios; este módulo se encarga de los burst limits
por minuto para no superar las APIs.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from alpha_agent.config import LLM


_lock = threading.Lock()
_history: dict[str, deque[float]] = defaultdict(deque)


def _limit_for(provider: str) -> int:
    return {
        "anthropic": LLM.rate_limit_anthropic_per_min,
        "groq": LLM.rate_limit_groq_per_min,
        "gemini": LLM.rate_limit_gemini_per_min,
        "deepseek": LLM.rate_limit_deepseek_per_min,
        "openrouter": LLM.rate_limit_openrouter_per_min,
    }.get(provider, 60)


def try_acquire(provider: str) -> bool:
    """Intenta consumir 1 slot del bucket de `provider` para esta ventana de 60s.

    Returns:
        True si hay capacidad y se consumió (no bloquea).
        False si la ventana ya está saturada — el gateway pasa al siguiente
        provider en la cascada.
    """
    now = time.time()
    cutoff = now - 60.0
    limit = _limit_for(provider)
    with _lock:
        q = _history[provider]
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


def get_remaining(provider: str) -> int:
    """Cuántos slots quedan en la ventana de 60s actual."""
    now = time.time()
    cutoff = now - 60.0
    limit = _limit_for(provider)
    with _lock:
        q = _history[provider]
        recent = sum(1 for t in q if t >= cutoff)
        return max(0, limit - recent)


def reset() -> None:
    """Reset todo el estado — usado en tests."""
    with _lock:
        _history.clear()
