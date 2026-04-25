"""
SQLite trade history — persiste cada orden ejecutada para P&L real y estadísticas.

Schema:
  id, ts, date, ticker, side, qty, price, notional,
  sleeve, status, order_id, stop_loss, take_profit,
  regime, vix, limit_price
"""

from __future__ import annotations

import sqlite3
import logging
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).resolve().parents[2] / "signals" / "trades.db"


@contextmanager
def _conn():
    con = sqlite3.connect(str(_DB_PATH))
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def _ensure_schema() -> None:
    with _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT NOT NULL,
                date        TEXT NOT NULL,
                ticker      TEXT NOT NULL,
                side        TEXT NOT NULL,
                qty         REAL,
                price       REAL,
                notional    REAL,
                sleeve      TEXT,
                status      TEXT,
                order_id    TEXT,
                stop_loss   REAL,
                take_profit REAL,
                regime      TEXT,
                vix         REAL,
                limit_price REAL
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_ticker ON trades(ticker)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_date ON trades(date)")


_ensure_schema()


def log_trade(
    *,
    ticker: str,
    side: str,
    qty: float | None = None,
    price: float | None = None,
    notional: float | None = None,
    sleeve: str | None = None,
    status: str = "submitted",
    order_id: str | None = None,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    regime: str | None = None,
    vix: float | None = None,
    limit_price: float | None = None,
) -> int:
    now = datetime.now()
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO trades
               (ts, date, ticker, side, qty, price, notional, sleeve, status,
                order_id, stop_loss, take_profit, regime, vix, limit_price)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                now.isoformat(timespec="seconds"),
                now.strftime("%Y-%m-%d"),
                ticker, side, qty, price, notional, sleeve, status,
                order_id, stop_loss, take_profit, regime, vix, limit_price,
            ),
        )
        row_id = cur.lastrowid
    logger.debug("trade_db: logged %s %s id=%d", side, ticker, row_id)
    return row_id


def get_trades(
    *,
    ticker: str | None = None,
    since: str | None = None,
    limit: int = 500,
) -> list[dict]:
    with _conn() as con:
        clauses, params = [], []
        if ticker:
            clauses.append("ticker = ?")
            params.append(ticker)
        if since:
            clauses.append("date >= ?")
            params.append(since)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = con.execute(
            f"SELECT * FROM trades {where} ORDER BY id DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
    return [dict(r) for r in rows]


def get_summary() -> dict:
    """Win-rate y P&L summary básico."""
    with _conn() as con:
        total = con.execute("SELECT COUNT(*) FROM trades WHERE side='sell'").fetchone()[0]
        rows = con.execute(
            "SELECT ticker, notional, price FROM trades ORDER BY id"
        ).fetchall()
    return {"total_sells": total, "total_trades": len(rows)}
