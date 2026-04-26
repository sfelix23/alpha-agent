"""
SQLite trade history — persiste cada orden ejecutada para P&L real y estadísticas.

Schema trades:
  id, ts, date, ticker, side, qty, price, notional,
  sleeve, status, order_id, stop_loss, take_profit,
  regime, vix, limit_price,
  closed_at, exit_price, pnl_usd, pnl_pct, hold_days
"""

from __future__ import annotations

import sqlite3
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta
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
                limit_price REAL,
                closed_at   TEXT,
                exit_price  REAL,
                pnl_usd     REAL,
                pnl_pct     REAL,
                hold_days   REAL
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_ticker ON trades(ticker)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_date ON trades(date)")
        # Migración: agregar columnas de cierre a DBs existentes sin recrear
        for col, typedef in [
            ("closed_at",  "TEXT"),
            ("exit_price", "REAL"),
            ("pnl_usd",    "REAL"),
            ("pnl_pct",    "REAL"),
            ("hold_days",  "REAL"),
        ]:
            try:
                con.execute(f"ALTER TABLE trades ADD COLUMN {col} {typedef}")
            except Exception:
                pass  # columna ya existe


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


def log_trade_close(
    *,
    ticker: str,
    exit_price: float,
    pnl_usd: float,
    pnl_pct: float,
) -> bool:
    """
    Marca como cerrado el BUY abierto más reciente del ticker.
    Devuelve True si encontró y actualizó un registro.
    """
    now = datetime.now()
    with _conn() as con:
        row = con.execute(
            """SELECT id, ts FROM trades
               WHERE ticker = ? AND side = 'BUY' AND closed_at IS NULL
               ORDER BY id DESC LIMIT 1""",
            (ticker,),
        ).fetchone()
        if not row:
            logger.debug("trade_db: no open BUY found for %s", ticker)
            return False
        entry_ts = datetime.fromisoformat(row["ts"])
        hold_days = round((now - entry_ts).total_seconds() / 86400, 2)
        con.execute(
            """UPDATE trades
               SET closed_at=?, exit_price=?, pnl_usd=?, pnl_pct=?, hold_days=?
               WHERE id=?""",
            (now.isoformat(timespec="seconds"), exit_price, pnl_usd, pnl_pct, hold_days, row["id"]),
        )
    logger.info("trade_db: closed %s exit=%.2f pnl=%+.2f (%.1fd)", ticker, exit_price, pnl_usd, hold_days)
    return True


def get_recent_stopouts(hours: int = 36) -> set[str]:
    """Tickers cerrados con pérdida en las últimas N horas (cooldown para evitar re-entrada)."""
    since = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    with _conn() as con:
        rows = con.execute(
            "SELECT ticker FROM trades WHERE side='BUY' AND closed_at >= ? AND pnl_usd < 0",
            (since,),
        ).fetchall()
    return {r["ticker"] for r in rows}


def get_summary() -> dict:
    """Win-rate, P&L promedio y estadísticas de trades cerrados."""
    with _conn() as con:
        total_open = con.execute(
            "SELECT COUNT(*) FROM trades WHERE side='BUY' AND closed_at IS NULL"
        ).fetchone()[0]
        closed = con.execute(
            """SELECT pnl_usd, pnl_pct, hold_days, ticker
               FROM trades WHERE side='BUY' AND closed_at IS NOT NULL"""
        ).fetchall()

    closed_list = [dict(r) for r in closed]
    n = len(closed_list)
    if n == 0:
        return {"open_positions": total_open, "closed_trades": 0,
                "win_rate": None, "avg_pnl_usd": None, "avg_pnl_pct": None,
                "total_pnl_usd": None, "avg_hold_days": None}

    wins = sum(1 for r in closed_list if (r["pnl_usd"] or 0) > 0)
    total_pnl = sum(r["pnl_usd"] or 0 for r in closed_list)
    avg_pnl   = total_pnl / n
    avg_pct   = sum(r["pnl_pct"] or 0 for r in closed_list) / n
    avg_hold  = sum(r["hold_days"] or 0 for r in closed_list) / n

    return {
        "open_positions": total_open,
        "closed_trades":  n,
        "win_rate":       round(wins / n, 3),
        "avg_pnl_usd":    round(avg_pnl, 2),
        "avg_pnl_pct":    round(avg_pct, 2),
        "total_pnl_usd":  round(total_pnl, 2),
        "avg_hold_days":  round(avg_hold, 2),
    }
