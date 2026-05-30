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
    """Conexión SQLite con WAL + busy_timeout para resistir escritura concurrente.

    Sin WAL, escrituras paralelas desde varios procesos (analyst + monitor +
    daytrader corriendo a la vez en Cloud Run / GHA) pueden corromper la
    base. WAL permite múltiples readers + 1 writer sin bloquear, y
    busy_timeout=15s evita "database is locked" en bursts.
    """
    con = sqlite3.connect(str(_DB_PATH), timeout=15.0)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=15000")
        con.execute("PRAGMA synchronous=NORMAL")
        yield con
        con.commit()
    finally:
        con.close()


def init_db() -> None:
    """Crea la tabla `trades` y aplica migraciones de columnas. Idempotente.

    Antes corría en module-load (linea final del archivo), con potencial race
    si dos procesos importaban el módulo al mismo tiempo. Ahora se invoca
    explícitamente desde cada run_*.py al arrancar.
    """
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
        # Migración: agregar columnas a DBs existentes sin recrear.
        # Estrechamos el except a OperationalError + verificación de "duplicate column"
        # para no swallowear errores reales (DB corrupta, permisos, FS lleno).
        for col, typedef in [
            ("closed_at",    "TEXT"),
            ("exit_price",   "REAL"),
            ("pnl_usd",      "REAL"),
            ("pnl_pct",      "REAL"),
            ("hold_days",    "REAL"),
            ("signals_json", "TEXT"),
        ]:
            try:
                con.execute(f"ALTER TABLE trades ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise

        # iter23: backfill — limpiar hold_days NEGATIVOS de datos viejos (pre-fix
        # iter18 emparejaba SELLs con BUYs posteriores → hold negativo, ensuciaba el
        # avg_hold del reporte semanal "-2.3d/-0.9d"). No sabemos el hold real de esos
        # trades mal-pareados → los clampeamos a 0. Idempotente.
        try:
            con.execute("UPDATE trades SET hold_days = 0 WHERE hold_days < 0")
        except sqlite3.OperationalError:
            pass


# Init en module-load — mantiene compatibilidad con el patrón previo.
# init_db() también es público por si run_*.py quieren llamarlo explícito.
init_db()


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
    signals_json: str | None = None,
) -> int:
    now = datetime.now()
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO trades
               (ts, date, ticker, side, qty, price, notional, sleeve, status,
                order_id, stop_loss, take_profit, regime, vix, limit_price, signals_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                now.isoformat(timespec="seconds"),
                now.strftime("%Y-%m-%d"),
                ticker, side, qty, price, notional, sleeve, status,
                order_id, stop_loss, take_profit, regime, vix, limit_price, signals_json,
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


def get_open_dt_tickers() -> set[str]:
    """Tickers con posicion DT abierta hoy (sleeve='DT', sin cerrar)."""
    today = datetime.now().strftime("%Y-%m-%d")
    with _conn() as con:
        rows = con.execute(
            "SELECT ticker FROM trades WHERE sleeve='DT' AND side='BUY' AND closed_at IS NULL AND date=?",
            (today,),
        ).fetchall()
    return {r["ticker"] for r in rows}


def sync_fills_from_alpaca(broker) -> int:
    """
    Consulta Alpaca por órdenes recientes y actualiza status 'submitted' → 'filled'
    en trade_db cuando el broker confirma ejecución.
    Retorna número de registros actualizados.
    """
    updated = 0
    try:
        orders = broker.list_filled_orders(limit=100)
    except Exception as e:
        logger.debug("sync_fills_from_alpaca: broker error %s", e)
        return 0

    alpaca_filled = {}  # order_id → avg_fill_price
    for o in orders:
        oid = getattr(o, "id", None)
        fill_price = getattr(o, "filled_avg_price", None)
        if oid and fill_price:
            try:
                alpaca_filled[str(oid)] = float(fill_price)
            except (TypeError, ValueError):
                pass

    if not alpaca_filled:
        return 0

    with _conn() as con:
        rows = con.execute(
            "SELECT id, order_id FROM trades WHERE status='submitted' AND order_id IS NOT NULL"
        ).fetchall()
        for row in rows:
            oid = row["order_id"]
            if oid in alpaca_filled:
                fill_px = alpaca_filled[oid]
                con.execute(
                    "UPDATE trades SET status='filled', price=? WHERE id=?",
                    (fill_px, row["id"]),
                )
                updated += 1

    if updated:
        logger.info("sync_fills_from_alpaca: %d trades actualizados a 'filled'", updated)
    return updated


def rebuild_ledger_from_alpaca(broker) -> dict:
    """iter45: reconstruye el ledger completo desde las actividades de Alpaca
    (fuente de verdad). El problema: stops/TPs/rebalancer/iter40-retries mandan
    órdenes directo a Alpaca SIN loguear a trade_db → solo ~26% de los sells
    quedaban registrados → P&L realizado sub-contado en ~95%.

    Estrategia: trae TODOS los fills, inserta los que faltan (dedup por order_id,
    preservando la metadata de las filas que el sistema ya logueó), resetea la
    reconciliación y re-corre el FIFO sobre el set completo. Idempotente.

    Retorna {'inserted': n, 'closed': n, 'realized_pnl': float}.
    """
    fills = broker.list_fill_activities()
    if not fills:
        logger.warning("rebuild_ledger: Alpaca no devolvió fills")
        return {"inserted": 0, "closed": 0, "realized_pnl": 0.0}

    with _conn() as con:
        # Columna activity_id para dedup (defensivo: ignora si ya existe)
        try:
            con.execute("ALTER TABLE trades ADD COLUMN activity_id TEXT")
        except Exception:
            pass
        existing_oids = {
            r["order_id"] for r in con.execute(
                "SELECT DISTINCT order_id FROM trades WHERE order_id IS NOT NULL"
            ).fetchall()
        }
        existing_acts = {
            r["activity_id"] for r in con.execute(
                "SELECT DISTINCT activity_id FROM trades WHERE activity_id IS NOT NULL"
            ).fetchall()
        }
        inserted = 0
        for f in fills:
            oid = f.get("order_id")
            aid = f.get("id")
            if oid in existing_oids or aid in existing_acts:
                continue  # ya registrado por el sistema o por un rebuild previo
            try:
                qty = float(f.get("qty") or 0)
                price = float(f.get("price") or 0)
            except (TypeError, ValueError):
                continue
            if qty <= 0 or price <= 0:
                continue
            side = "BUY" if str(f.get("side", "")).lower().startswith("buy") else "SELL"
            tx = str(f.get("transaction_time", ""))
            ts = tx.replace("Z", "")[:19] if tx else datetime.now().isoformat(timespec="seconds")
            date_str = ts[:10]
            con.execute(
                """INSERT INTO trades
                   (ts, date, ticker, side, qty, price, notional, status, order_id, activity_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (ts, date_str, f.get("symbol"), side, qty, price, qty * price,
                 "filled", oid, aid),
            )
            existing_oids.add(oid)
            existing_acts.add(aid)
            inserted += 1

        # Reset de reconciliación → recomputar FIFO limpio sobre el set completo
        con.execute(
            "UPDATE trades SET closed_at=NULL, exit_price=NULL, pnl_usd=NULL, "
            "pnl_pct=NULL, hold_days=NULL WHERE side='BUY'"
        )

    closed = reconcile_buy_sell_pairs()
    realized = sum(
        (t.get("pnl_usd") or 0)
        for t in get_trades(limit=1000)
        if t.get("side") == "BUY" and t.get("pnl_usd") is not None
    )
    logger.info(
        "rebuild_ledger: +%d fills insertados, %d BUYs cerrados, realizado=$%.2f",
        inserted, closed, realized,
    )
    return {"inserted": inserted, "closed": closed, "realized_pnl": round(realized, 2)}


def reconcile_buy_sell_pairs() -> int:
    """
    Matches unprocessed SELL rows to open BUY rows (FIFO per ticker).
    Updates BUY rows with closed_at, exit_price, pnl_usd, pnl_pct, hold_days.
    Returns number of BUY rows closed.
    """
    closed_count = 0
    with _conn() as con:
        # Find all SELL rows that have no corresponding BUY close yet
        sells = con.execute(
            "SELECT id, ticker, qty, price, ts FROM trades WHERE side='SELL' ORDER BY id ASC"
        ).fetchall()

        for sell in sells:
            ticker = sell["ticker"]
            sell_qty = sell["qty"] or 0.0
            sell_price = sell["price"] or 0.0
            sell_ts = sell["ts"]

            if sell_qty <= 0:
                continue

            # Find oldest open BUY(s) for this ticker (FIFO).
            # iter18 fix: SOLO BUYs anteriores o iguales al SELL (ts <= sell_ts).
            # Antes matcheaba cualquier BUY abierto → emparejaba un SELL con un BUY
            # POSTERIOR y producía hold_days negativo (cerrado antes de abierto),
            # corrompiendo todas las métricas de hold/win.
            buys = con.execute(
                "SELECT id, qty, price, ts FROM trades "
                "WHERE ticker=? AND side='BUY' AND closed_at IS NULL AND ts <= ? "
                "ORDER BY id ASC",
                (ticker, sell_ts),
            ).fetchall()

            remaining_sell_qty = sell_qty
            for buy in buys:
                if remaining_sell_qty <= 0:
                    break
                buy_qty = buy["qty"] or 0.0
                buy_price = buy["price"] or 0.0
                buy_ts = buy["ts"]

                if buy_qty <= 0:
                    continue

                closed_qty = min(buy_qty, remaining_sell_qty)
                pnl_usd = round((sell_price - buy_price) * closed_qty, 2)
                pnl_pct = round((sell_price / buy_price - 1) * 100, 2) if buy_price > 0 else 0.0
                try:
                    buy_dt = datetime.fromisoformat(buy_ts)
                    sell_dt = datetime.fromisoformat(sell_ts)
                    hold_days = max(0.0, round((sell_dt - buy_dt).total_seconds() / 86400, 2))
                except Exception:
                    hold_days = 0.0

                con.execute(
                    "UPDATE trades SET closed_at=?, exit_price=?, pnl_usd=?, pnl_pct=?, hold_days=? WHERE id=?",
                    (sell_ts, sell_price, pnl_usd, pnl_pct, hold_days, buy["id"]),
                )
                closed_count += 1
                remaining_sell_qty -= closed_qty

    if closed_count:
        logger.info("reconcile_buy_sell_pairs: closed %d BUY rows", closed_count)
    return closed_count


def get_attribution() -> dict:
    """
    Agrupa trades cerrados por régimen, sleeve y banda de VIX.
    Usa datos ya almacenados — no requiere cambios al pipeline.
    Retorna win_rate, avg_pnl y n por grupo para identificar qué contextos generan alfa.
    """
    from collections import defaultdict
    with _conn() as con:
        rows = con.execute(
            "SELECT regime, sleeve, vix, pnl_usd FROM trades "
            "WHERE side='BUY' AND pnl_usd IS NOT NULL"
        ).fetchall()

    groups: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        pnl    = r["pnl_usd"] or 0.0
        regime = (r["regime"] or "UNKNOWN").upper()
        sleeve = r["sleeve"] or "?"
        vix    = float(r["vix"] or 0.0)

        groups[f"regime:{regime}"].append(pnl)
        groups[f"sleeve:{sleeve}"].append(pnl)
        vix_band = "vix:alto(>22)" if vix > 22 else ("vix:bajo(<15)" if vix < 15 else "vix:medio(15-22)")
        groups[vix_band].append(pnl)

    result = {}
    for key, pnls in sorted(groups.items()):
        n    = len(pnls)
        wins = sum(1 for p in pnls if p > 0)
        result[key] = {
            "n":         n,
            "win_rate":  round(wins / n, 2) if n else 0.0,
            "avg_pnl":   round(sum(pnls) / n, 2) if n else 0.0,
            "total_pnl": round(sum(pnls), 2),
        }
    return result


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


# ─────────────────────────────────────────────────────────────────────────────
# Capital coordination entre sleeves (Sesión 4 del plan)
#
# Las 3 cuentas Alpaca (LP/CP, scalper, daytrader) operan independientes pero
# el daytrader tenía un DT_BUDGET=1500 hardcodeado. Si LP/CP toman capital,
# el daytrader puede pedir órdenes que excedan buying_power.
#
# Estas funciones persisten reservas en signals/capital_reservations.json
# para que cada sleeve consulte cuánto le queda antes de operar.
# ─────────────────────────────────────────────────────────────────────────────

import json as _json
from datetime import datetime as _datetime

_CAPITAL_PATH = _DB_PATH.parent / "capital_reservations.json"


def _load_capital() -> dict:
    if not _CAPITAL_PATH.exists():
        return {"reservations": {}, "updated_at": None}
    try:
        return _json.loads(_CAPITAL_PATH.read_text(encoding="utf-8"))
    except (_json.JSONDecodeError, OSError) as e:
        logger.warning("capital_reservations corrupto (%s) — reset", e)
        return {"reservations": {}, "updated_at": None}


def _save_capital(data: dict) -> None:
    data["updated_at"] = _datetime.utcnow().isoformat(timespec="seconds")
    tmp = _CAPITAL_PATH.with_suffix(".json.tmp")
    tmp.write_text(_json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(_CAPITAL_PATH)


def reserve_capital(sleeve: str, amount: float) -> bool:
    """Marca `amount` USD como reservado para este sleeve. Idempotente — pisa el valor.

    Llamar al arrancar cada agente con el budget que necesita. El próximo
    `available_capital()` lo descuenta del buying_power total.
    """
    data = _load_capital()
    data["reservations"][sleeve] = {
        "amount": float(amount),
        "since": _datetime.utcnow().isoformat(timespec="seconds"),
    }
    _save_capital(data)
    logger.info("capital: %s reservó $%.2f", sleeve, amount)
    return True


def release_capital(sleeve: str) -> None:
    """Libera la reserva de un sleeve (al cerrar todas sus posiciones)."""
    data = _load_capital()
    if data["reservations"].pop(sleeve, None):
        _save_capital(data)
        logger.info("capital: %s liberó reserva", sleeve)


def available_capital(broker, sleeve: str) -> float:
    """USD disponibles para `sleeve` = buying_power − reservas de OTROS sleeves.

    Args:
        broker: AlpacaBroker con get_buying_power() o get_equity().
        sleeve: identificador del sleeve consultando ("LP", "CP", "DT", "SCALP").

    Returns:
        USD disponibles. Si el broker falla, devuelve 0 (defensivo).
    """
    try:
        if hasattr(broker, "get_buying_power"):
            total = float(broker.get_buying_power())
        else:
            total = float(broker.get_equity())
    except Exception as e:
        logger.warning("available_capital: broker error (%s) — retornando 0", e)
        return 0.0

    data = _load_capital()
    reserved_by_others = sum(
        r.get("amount", 0.0)
        for s, r in data["reservations"].items()
        if s != sleeve
    )
    return max(0.0, total - reserved_by_others)


def get_capital_snapshot() -> dict:
    """Para el dashboard."""
    return _load_capital()


# ─────────────────────────────────────────────────────────────────────────────
# Multi-account aggregator (Sesión 5 del plan)
#
# Combina P&L, equity y Sharpe rolling de las 3 cuentas Alpaca (LP/CP,
# scalper, daytrader) en una vista unificada. Esto permite ver el sistema
# entero como un portfolio de estrategias non-correlated, y eventualmente
# rotar capital al sleeve que mejor performa.
# ─────────────────────────────────────────────────────────────────────────────


def rolling_sharpe_by_sleeve(days: int = 30) -> dict[str, dict]:
    """Sharpe ratio rolling por sleeve usando los trades cerrados de trade_db.

    Args:
        days: ventana en días para calcular el Sharpe.

    Returns:
        Dict {sleeve: {n_trades, avg_pnl, std_pnl, sharpe, win_rate}}.
    """
    from collections import defaultdict
    from datetime import timedelta as _td

    since = (_datetime.now() - _td(days=days)).strftime("%Y-%m-%d")
    with _conn() as con:
        rows = con.execute(
            "SELECT sleeve, pnl_usd, pnl_pct FROM trades "
            "WHERE side='BUY' AND closed_at IS NOT NULL AND date(closed_at) >= ?",
            (since,),
        ).fetchall()

    by_sleeve: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        sleeve = r["sleeve"] or "UNKNOWN"
        pnl = r["pnl_pct"] or 0.0
        by_sleeve[sleeve].append(pnl)

    result = {}
    for sleeve, pnls in by_sleeve.items():
        n = len(pnls)
        if n < 2:
            result[sleeve] = {"n_trades": n, "avg_pnl": 0.0, "std_pnl": 0.0, "sharpe": 0.0, "win_rate": 0.0}
            continue
        avg = sum(pnls) / n
        var = sum((p - avg) ** 2 for p in pnls) / (n - 1)
        std = var ** 0.5
        sharpe = (avg / std) if std > 0 else 0.0
        wins = sum(1 for p in pnls if p > 0)
        result[sleeve] = {
            "n_trades": n,
            "avg_pnl": round(avg, 2),
            "std_pnl": round(std, 2),
            "sharpe": round(sharpe, 2),
            "win_rate": round(wins / n, 2),
        }
    return result


def get_combined_state(brokers: dict | None = None) -> dict:
    """Snapshot agregado de las 3 cuentas Alpaca + P&L por sleeve.

    Args:
        brokers: dict opcional {account_id: broker}. Si es None, lee sólo
            de trade_db (sin tocar Alpaca). Útil para el dashboard que no
            quiere depender de red.

    Returns:
        Dict con:
          - total_equity (None si brokers=None)
          - by_account: {main, scalp, dt} con equity y posiciones
          - by_sleeve: P&L y Sharpe rolling por sleeve
          - capital_reservations: estado del capital_manager
          - summary: stats del trade_db (open_positions, win_rate, total_pnl_usd)
    """
    state: dict = {
        "total_equity": None,
        "by_account": {},
        "by_sleeve": rolling_sharpe_by_sleeve(30),
        "capital_reservations": _load_capital().get("reservations", {}),
        "summary": get_summary(),
    }

    if brokers:
        total = 0.0
        for account_id, broker in brokers.items():
            try:
                equity = float(broker.get_equity())
                positions = broker.get_positions() if hasattr(broker, "get_positions") else []
                state["by_account"][account_id] = {
                    "equity": equity,
                    "n_positions": len(positions),
                }
                total += equity
            except Exception as e:
                logger.warning("aggregator: broker %s error (%s)", account_id, e)
                state["by_account"][account_id] = {"equity": None, "error": str(e)}
        state["total_equity"] = round(total, 2) if total > 0 else None

    return state


# ─────────────────────────────────────────────────────────────────────────────
# SEGUNDO CEREBRO — memoria de trades / post-mortems (iter13)
#
# Filosofía: el sistema debe APRENDER de sus operaciones pasadas, no repetir
# errores. En vez de Obsidian (notas humanas que el bot no puede leer), la
# memoria vive en la misma SQLite de trades y se DERIVA de los trades cerrados
# — sin tabla nueva, sin migración, funciona con el historial existente.
#
# - get_ticker_memory(ticker): historial + veredicto + lección de ESE ticker.
# - memory_score_adjustment(ticker): multiplicador para el scorer (sube los que
#   históricamente ganan acá, baja/evita los que cronicamente pierden).
# - summarize_learnings(): top lecciones legibles para el brief y el dashboard.
# ─────────────────────────────────────────────────────────────────────────────


def get_ticker_memory(ticker: str, lookback_days: int = 240) -> dict:
    """Memoria de un ticker: cómo le fue a ESTE sistema operándolo.

    Returns dict con n, win_rate, avg_pnl_pct, avg_hold_days, last_outcome,
    last_pnl_pct, bias ('favorable'|'adverso'|'neutral') y lesson (texto).
    bias='neutral' si hay <3 trades cerrados (sin evidencia suficiente).
    """
    since = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    with _conn() as con:
        rows = con.execute(
            """SELECT pnl_pct, pnl_usd, hold_days, closed_at, regime
               FROM trades
               WHERE ticker=? AND side='BUY' AND closed_at IS NOT NULL AND date >= ?
               ORDER BY closed_at ASC""",
            (ticker, since),
        ).fetchall()

    n = len(rows)
    base = {
        "ticker": ticker, "n": n, "win_rate": None, "avg_pnl_pct": None,
        "avg_hold_days": None, "last_outcome": None, "last_pnl_pct": None,
        "bias": "neutral", "lesson": "",
    }
    if n == 0:
        return base

    pcts = [r["pnl_pct"] or 0.0 for r in rows]
    holds = [r["hold_days"] or 0.0 for r in rows]
    wins = sum(1 for p in pcts if p > 0)
    win_rate = round(wins / n, 2)
    avg_pct = round(sum(pcts) / n, 2)
    avg_hold = round(sum(holds) / n, 1)
    last = rows[-1]
    last_pct = round(last["pnl_pct"] or 0.0, 2)
    last_outcome = "WIN" if last_pct > 0 else ("LOSS" if last_pct < 0 else "FLAT")

    base.update({
        "win_rate": win_rate, "avg_pnl_pct": avg_pct, "avg_hold_days": avg_hold,
        "last_outcome": last_outcome, "last_pnl_pct": last_pct,
    })

    # Veredicto sólo con evidencia suficiente (>=3 trades)
    if n >= 3:
        if win_rate >= 0.60 and avg_pct > 0:
            base["bias"] = "favorable"
            base["lesson"] = (f"{ticker}: histórico FAVORABLE — {win_rate:.0%} win, "
                              f"{avg_pct:+.1f}% prom en {n} trades. Sistema lo opera bien.")
        elif win_rate <= 0.34 and avg_pct < 0:
            base["bias"] = "adverso"
            base["lesson"] = (f"{ticker}: histórico ADVERSO — sólo {win_rate:.0%} win, "
                              f"{avg_pct:+.1f}% prom en {n} trades. Reducir tamaño o evitar.")
        else:
            base["bias"] = "neutral"
            base["lesson"] = (f"{ticker}: mixto — {win_rate:.0%} win, {avg_pct:+.1f}% prom ({n} trades).")
    return base


def memory_score_adjustment(ticker: str) -> float:
    """Multiplicador de score basado en la memoria del ticker (segundo cerebro).

    favorable → 1.12 (sube ganadores históricos)
    adverso   → 0.82 (baja perdedores crónicos)
    neutral   → 1.00
    Devuelve 1.0 ante cualquier error (nunca rompe el scorer).
    """
    try:
        mem = get_ticker_memory(ticker)
        return {"favorable": 1.12, "adverso": 0.82}.get(mem["bias"], 1.0)
    except Exception as exc:
        logger.debug("memory_score_adjustment(%s): %s", ticker, exc)
        return 1.0


def summarize_learnings(limit: int = 8, lookback_days: int = 240) -> list[str]:
    """Top lecciones legibles para el brief WhatsApp y el dashboard.

    Prioriza tickers con veredicto fuerte (favorable/adverso) y más trades.
    """
    since = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    with _conn() as con:
        tickers = [r["ticker"] for r in con.execute(
            """SELECT ticker, COUNT(*) c FROM trades
               WHERE side='BUY' AND closed_at IS NOT NULL AND date >= ?
               GROUP BY ticker HAVING c >= 3 ORDER BY c DESC""",
            (since,),
        ).fetchall()]

    lessons: list[tuple[int, str]] = []
    for t in tickers:
        mem = get_ticker_memory(t, lookback_days)
        if mem["bias"] in ("favorable", "adverso") and mem["lesson"]:
            # ordena: adverso primero (más accionable), luego por nº de trades
            rank = (0 if mem["bias"] == "adverso" else 1, -mem["n"])
            lessons.append((rank, mem["lesson"]))
    lessons.sort(key=lambda x: x[0])
    return [l for _, l in lessons[:limit]]
