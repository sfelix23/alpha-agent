"""Cache de respuestas LLM en SQLite con TTL por purpose.

Las llamadas LLM idénticas (mismo prompt + mismo modelo + mismo max_tokens)
devuelven la misma respuesta en una ventana de tiempo definida por el
`purpose` (ej. sentiment 24h, assess_position 30min). Esto reduce el volumen
de llamadas drásticamente — el monitor cada 30min sobre 4-6 posiciones
abiertas pega 90% cache hit en la mayoría de los días.

Almacenado en `signals/llm_cache.sqlite` (se commitea al repo para que
sobreviva entre Cloud Run Jobs efímeros).
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from alpha_agent.config import LLM, PATHS

logger = logging.getLogger(__name__)

_DB_PATH = PATHS.signals_dir / "llm_cache.sqlite"


def _ttl_seconds(purpose: str) -> float:
    table = {
        "sentiment": LLM.cache_ttl_sentiment_h,
        "event_score": LLM.cache_ttl_event_score_h,
        "assess_position": LLM.cache_ttl_assess_position_h,
        "narrative": LLM.cache_ttl_narrative_h,
        "wall_street": LLM.cache_ttl_wall_street_h,
        "risk_debate": LLM.cache_ttl_risk_debate_h,
    }
    return table.get(purpose, 1.0) * 3600.0


def cache_key(prompt: str, model: str, max_tokens: int, extra: str = "") -> str:
    """SHA-256 de los inputs que definen una respuesta determinista."""
    payload = f"{model}|{max_tokens}|{extra}|{prompt}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@contextmanager
def _conn():
    con = sqlite3.connect(str(_DB_PATH), timeout=10.0)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=10000")
        yield con
        con.commit()
    finally:
        con.close()


def init_cache() -> None:
    """Crea la tabla si no existe. Idempotente."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _conn() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_cache (
                key         TEXT PRIMARY KEY,
                purpose     TEXT NOT NULL,
                provider    TEXT NOT NULL,
                model       TEXT NOT NULL,
                response    TEXT NOT NULL,
                tokens_in   INTEGER,
                tokens_out  INTEGER,
                created_at  REAL NOT NULL
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_cache_purpose ON llm_cache(purpose)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_cache_created ON llm_cache(created_at)")


# Inicialización lazy — al primer get/put.
_initialized = False


def _ensure_init() -> None:
    global _initialized
    if not _initialized:
        init_cache()
        _initialized = True


def get(key: str, purpose: str) -> tuple[str, str, str] | None:
    """Devuelve (response, provider, model) si el cache hit es fresco, None si no."""
    _ensure_init()
    ttl = _ttl_seconds(purpose)
    cutoff = time.time() - ttl
    try:
        with _conn() as con:
            row = con.execute(
                "SELECT response, provider, model FROM llm_cache "
                "WHERE key = ? AND created_at >= ?",
                (key, cutoff),
            ).fetchone()
            if row:
                return row[0], row[1], row[2]
    except sqlite3.Error as e:
        logger.warning("llm_cache: error de SQLite en get (%s)", e)
    return None


def put(
    key: str,
    *,
    purpose: str,
    provider: str,
    model: str,
    response: str,
    tokens_in: int,
    tokens_out: int,
) -> None:
    _ensure_init()
    try:
        with _conn() as con:
            con.execute(
                "INSERT OR REPLACE INTO llm_cache "
                "(key, purpose, provider, model, response, tokens_in, tokens_out, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (key, purpose, provider, model, response, tokens_in, tokens_out, time.time()),
            )
    except sqlite3.Error as e:
        logger.warning("llm_cache: error de SQLite en put (%s)", e)


def prune_expired(max_age_hours: float = 48.0) -> int:
    """Borra entradas más viejas que max_age_hours. Devuelve cuántas borró.

    El gateway lo llama una vez al día (idempotente y barato) para no acumular
    indefinidamente. El TTL real lo controla `get()` por purpose; esto es
    sólo housekeeping del archivo SQLite.
    """
    _ensure_init()
    cutoff = time.time() - max_age_hours * 3600.0
    try:
        with _conn() as con:
            cur = con.execute("DELETE FROM llm_cache WHERE created_at < ?", (cutoff,))
            return cur.rowcount or 0
    except sqlite3.Error as e:
        logger.warning("llm_cache: error en prune (%s)", e)
        return 0


def get_stats() -> dict:
    """Stats para el dashboard."""
    _ensure_init()
    try:
        with _conn() as con:
            total = con.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0]
            by_purpose = dict(
                con.execute(
                    "SELECT purpose, COUNT(*) FROM llm_cache GROUP BY purpose"
                ).fetchall()
            )
            by_provider = dict(
                con.execute(
                    "SELECT provider, COUNT(*) FROM llm_cache GROUP BY provider"
                ).fetchall()
            )
            return {"total_entries": total, "by_purpose": by_purpose, "by_provider": by_provider}
    except sqlite3.Error:
        return {"total_entries": 0, "by_purpose": {}, "by_provider": {}}
