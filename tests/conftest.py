"""Fixtures compartidos — redirigen los JSONs/SQLites del LLM a tmp_path."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from alpha_agent.news import claude_analyst as ca


@pytest.fixture(autouse=True)
def isolated_llm_state(tmp_path, monkeypatch):
    """Redirige budget, cache, state a tmp_path para cada test."""
    signals_dir = tmp_path / "signals"
    logs_dir = tmp_path / "logs"
    signals_dir.mkdir()
    logs_dir.mkdir()

    monkeypatch.setattr(ca, "_BUDGET_PATH", signals_dir / "llm_budget.json")
    monkeypatch.setattr(ca, "_STATE_PATH", signals_dir / "llm_provider_state.json")
    monkeypatch.setattr(ca, "_CACHE_DB_PATH", signals_dir / "llm_cache.sqlite")
    monkeypatch.setattr(ca, "_cache_initialized", False)

    # PATHS frozen — stub para budget archive.
    stub = SimpleNamespace(
        logs_dir=logs_dir,
        signals_dir=signals_dir,
        cache_dir=tmp_path / "cache",
    )
    stub.cache_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ca, "PATHS", stub)

    ca.rate_reset()
    yield
    ca.rate_reset()
