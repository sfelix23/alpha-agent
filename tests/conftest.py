"""Fixtures compartidos para los tests."""

from __future__ import annotations

import pytest

from alpha_agent.llm import budget, cache, rate_limit


@pytest.fixture(autouse=True)
def isolated_llm_state(tmp_path, monkeypatch):
    """Redirige budget, cache y state JSONs a tmp_path para cada test.

    Sin esto los tests pisan el `signals/llm_budget.json` real del repo.
    """
    signals_dir = tmp_path / "signals"
    logs_dir = tmp_path / "logs"
    signals_dir.mkdir()
    logs_dir.mkdir()

    monkeypatch.setattr(budget, "_BUDGET_PATH", signals_dir / "llm_budget.json")
    monkeypatch.setattr(budget, "_STATE_PATH", signals_dir / "llm_provider_state.json")
    monkeypatch.setattr(cache, "_DB_PATH", signals_dir / "llm_cache.sqlite")
    monkeypatch.setattr(cache, "_initialized", False)

    # PATHS es un dataclass frozen — reemplazamos con un stub para los módulos
    # que lo usan (budget.py archiva en PATHS.logs_dir).
    from types import SimpleNamespace
    cache_dir_test = tmp_path / "cache"
    cache_dir_test.mkdir(parents=True, exist_ok=True)
    stub = SimpleNamespace(
        logs_dir=logs_dir,
        signals_dir=signals_dir,
        cache_dir=cache_dir_test,
    )
    monkeypatch.setattr("alpha_agent.llm.budget.PATHS", stub)

    rate_limit.reset()
    yield
    rate_limit.reset()
