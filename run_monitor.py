"""
Agente 3 — Monitor intradía de posiciones abiertas.

Corre cada 30 minutos durante el horario de mercado (vía Task Scheduler).
Revisa todas las posiciones abiertas en Alpaca y actúa si:
    - Una posición tocó el stop loss → cierra
    - Una posición tocó el take profit → cierra
    - El equity total cayó más del kill switch % → cierra TODO
    - Una posición subió lo suficiente → trailing stop activado

Sólo manda WhatsApp cuando ACTÚA (cierra algo o detecta alerta crítica).
Si todo está en orden, loguea y sale silenciosamente.

Uso:
    python run_monitor.py              # modo default (dry-run seguro)
    python run_monitor.py --live       # ejecuta cierres reales en Alpaca
    python run_monitor.py --dry-run    # loguea pero no cierra nada
"""

from __future__ import annotations

import sys

# Fix para Windows cp1252: forzar stdout/stderr a UTF-8
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.platform == "win32" and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from filelock import FileLock, Timeout as FileLockTimeout

from alpha_agent.config import setup_agent_logging
from alpha_agent.news.claude_analyst import assess_position as claude_assess

# ── Rutas absolutas (funciona sin importar el working directory de Task Scheduler)
BASE_DIR = Path(__file__).parent.resolve()
SIGNALS_PATH = BASE_DIR / "signals" / "latest.json"
LOG_DIR = BASE_DIR / "logs"

logger = logging.getLogger("monitor")


def setup_logging():
    """Wrapper compatible — delega al logging centralizado en config.py."""
    setup_agent_logging("monitor")


def load_signals_latest() -> dict:
    """Carga el último signals.json para obtener stops/TPs configurados."""
    if not SIGNALS_PATH.exists():
        logger.warning("signals/latest.json no encontrado en %s", SIGNALS_PATH)
        return {}
    try:
        return json.loads(SIGNALS_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Error leyendo signals: %s", e)
        return {}


def get_signal_for_ticker(signals_data: dict, ticker: str) -> dict | None:
    """Busca la señal del ticker en LP, CP, options."""
    for bucket in ("long_term", "short_term", "options_book", "hedge_book"):
        for sig in signals_data.get(bucket, []):
            if sig.get("ticker") == ticker:
                return sig
    return None


def current_price_from_position(pos) -> float:
    """
    Deriva el precio actual desde market_value / qty.
    Position tiene: ticker, qty, avg_price, market_value, unrealized_pl, asset_class
    """
    if pos.qty and abs(pos.qty) > 0:
        return pos.market_value / pos.qty
    return pos.avg_price  # fallback


def pnl_pct_from_position(pos) -> float:
    """Retorna el P&L % como número (ej: 5.3 = +5.3%)"""
    cost_basis = pos.avg_price * pos.qty
    if cost_basis and abs(cost_basis) > 0:
        return (pos.unrealized_pl / cost_basis) * 100.0
    return 0.0


def _update_trailing_stop(
    broker,
    ticker: str,
    new_stop: float,
    qty: float,
    live: bool,
    alerts: list,
    description: str,
) -> None:
    """Actualiza el stop loss en Alpaca y registra la alerta."""
    logger.info("Trailing stop %s: %s", ticker, description)
    alerts.append(f"📈 *{ticker}* trailing stop actualizado — {description}")
    if live:
        oid = broker.update_stop_loss(ticker, new_stop, qty)
        if oid:
            alerts.append(f"  ✅ Stop order enviado (id={oid})")
        else:
            alerts.append(f"  ⚠️ No se pudo enviar stop order")
    else:
        alerts.append("  _(dry-run: stop no enviado a Alpaca)_")


def _compute_chandelier_stop(ticker: str, regime: str = "LATERAL") -> float | None:
    """
    Chandelier Exit = highest_close(22) - N × ATR(22), donde N depende del régimen.

    Iter2: ATR multiplier por régimen:
        BULL    → 3.5 (stops anchos, deja correr trends largos)
        LATERAL → 2.8 (default — balance)
        BEAR    → 2.0 (stops tight, protege capital)

    Esto reemplaza el multiplier fijo 3.5 que era demasiado ancho en BEAR
    (dejaba caer las posiciones -7/-8% antes de cortar).

    Descarga últimos 30 días de OHLC via yfinance. Retorna None si no hay datos.
    """
    try:
        import numpy as np
        import yfinance as yf
        df = yf.download(ticker, period="35d", progress=False, auto_adjust=True, timeout=10)
        if df is None or len(df) < 23:
            return None
        close = df["Close"].squeeze()
        high  = df["High"].squeeze()
        low   = df["Low"].squeeze()
        # ATR(22)
        prev_close = close.shift(1)
        tr = (
            (high - low).abs()
            .combine((high - prev_close).abs(), max)
            .combine((low - prev_close).abs(), max)
        )
        atr22 = float(tr.rolling(22).mean().iloc[-1])
        highest_close_22 = float(close.tail(22).max())
        if np.isnan(atr22) or np.isnan(highest_close_22):
            return None
        r = regime.upper() if isinstance(regime, str) else "LATERAL"
        atr_mult = 2.0 if r == "BEAR" else (3.5 if r == "BULL" else 2.8)
        return round(highest_close_22 - atr_mult * atr22, 2)
    except Exception as e:
        logger.debug("chandelier_stop %s: %s", ticker, e)
        return None


def _parse_option_symbol(symbol: str) -> dict | None:
    """
    Parsea un símbolo OCC de Alpaca.
    Formato: {UNDERLYING}{YYMMDD}{C/P}{STRIKE*1000 padded 8 digits}
    Ejemplo: NVDA251219C00950000 → underlying=NVDA, expiry=2025-12-19, type=call, strike=950.0
    """
    import re
    from datetime import date as _d
    m = re.match(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$", symbol)
    if not m:
        return None
    underlying, ds, kind, sk = m.groups()
    try:
        expiry = _d(2000 + int(ds[:2]), int(ds[2:4]), int(ds[4:6]))
        strike = int(sk) / 1000.0
        return {
            "underlying": underlying,
            "expiry": expiry,
            "type": "call" if kind == "C" else "put",
            "strike": strike,
        }
    except Exception:
        return None


def _check_vix_spike() -> tuple[bool, float, float]:
    """
    Devuelve (spike_detected, vix_now, vix_prev).
    Spike = VIX intradía subió >20% vs el cierre anterior.
    Usa datos 5min para capturar el nivel real en tiempo de mercado,
    y datos diarios para el cierre previo confirmado.
    """
    try:
        import yfinance as yf
        # Nivel actual real: última barra de 5min intradía
        df_intra = yf.download("^VIX", period="2d", interval="5m",
                               progress=False, auto_adjust=True, timeout=10)
        # Cierre previo confirmado: barra diaria anterior (no la del día en curso)
        df_daily = yf.download("^VIX", period="5d", interval="1d",
                               progress=False, auto_adjust=True, timeout=10)
        if df_intra is None or len(df_intra) < 2:
            return False, 0.0, 0.0
        if df_daily is None or len(df_daily) < 2:
            return False, 0.0, 0.0
        vix_now  = float(df_intra["Close"].squeeze().iloc[-1])
        vix_prev = float(df_daily["Close"].squeeze().iloc[-2])
        spike = vix_now > vix_prev * 1.20
        return spike, round(vix_now, 1), round(vix_prev, 1)
    except Exception:
        return False, 0.0, 0.0


_VETO_CACHE_PATH = BASE_DIR / "signals" / "monitor_veto_cache.json"
_VETO_COOLDOWN_HOURS = 3  # después de un veto Claude, no re-evaluar por 3h


def _is_in_veto_cooldown(ticker: str) -> bool:
    """True si Claude vetó este ticker recientemente y el cooldown sigue activo."""
    try:
        if not _VETO_CACHE_PATH.exists():
            return False
        cache = json.loads(_VETO_CACHE_PATH.read_text(encoding="utf-8"))
        entry = cache.get(ticker)
        if not entry:
            return False
        until = datetime.fromisoformat(entry["until"])
        return datetime.now() < until
    except Exception:
        return False


def _is_post_veto_today(ticker: str) -> bool:
    """True si ya se vetó este ticker hoy pero el cooldown expiró.
    Permite un solo veto por posición por día — después ejecuta directamente
    sin re-consultar Claude, evitando el loop veto → 3h → veto → 3h."""
    try:
        if not _VETO_CACHE_PATH.exists():
            return False
        cache = json.loads(_VETO_CACHE_PATH.read_text(encoding="utf-8"))
        entry = cache.get(ticker)
        if not entry:
            return False
        until = datetime.fromisoformat(entry["until"])
        # Cooldown ya expiró Y fue registrado hoy
        return datetime.now() >= until and until.date() == datetime.now().date()
    except Exception:
        return False


def _register_veto(ticker: str, reason: str) -> None:
    """Registra un veto de Claude con expiry de VETO_COOLDOWN_HOURS horas."""
    from datetime import timedelta
    try:
        cache = {}
        if _VETO_CACHE_PATH.exists():
            cache = json.loads(_VETO_CACHE_PATH.read_text(encoding="utf-8"))
        until = (datetime.now() + timedelta(hours=_VETO_COOLDOWN_HOURS)).isoformat()
        cache[ticker] = {"until": until, "reason": reason[:120]}
        _VETO_CACHE_PATH.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except Exception:
        pass


def _was_partially_exited_today(ticker: str) -> bool:
    """Verifica si ya se hizo un partial TP exit hoy para evitar doble venta."""
    try:
        from alpha_agent.analytics.trade_db import get_trades
        today  = datetime.now().strftime("%Y-%m-%d")
        trades = get_trades(ticker=ticker, limit=20)
        return any(
            t.get("sleeve") == "PARTIAL_TP" and t.get("date") == today
            for t in trades
        )
    except Exception:
        return False


def _log_partial_exit(ticker: str, qty: float, price: float, pnl_usd: float) -> None:
    try:
        from alpha_agent.analytics.trade_db import log_trade
        log_trade(ticker=ticker, side="SELL", qty=qty, price=price,
                  notional=qty * price, sleeve="PARTIAL_TP", status="filled",
                  pnl_usd=pnl_usd)
    except Exception:
        pass


def _read_cp_max_hold_days() -> int:
    """Lee cp_max_hold_days del último allocation.json (escrito por el analyst)."""
    try:
        data = json.loads((BASE_DIR / "signals" / "allocation.json").read_text(encoding="utf-8"))
        return int(data.get("cp_max_hold_days", 3))
    except Exception:
        return 3


def _get_cp_entry_date(ticker: str) -> str | None:
    """Devuelve la fecha de entrada del trade CP abierto más reciente para el ticker."""
    try:
        from alpha_agent.analytics.trade_db import get_trades
        trades = get_trades(limit=300)
        for t in trades:
            if (t.get("ticker") == ticker
                    and t.get("sleeve") in ("CP", "MIX")
                    and t.get("closed_at") is None
                    and t.get("side") == "BUY"):
                return t.get("date")
    except Exception:
        pass
    return None


_SCALEIN_FILE = BASE_DIR / "signals" / "scale_ins.json"


def _was_scaled_in_today(ticker: str) -> bool:
    """True si ya se ejecutó un scale-in hoy para este ticker."""
    from datetime import date
    try:
        data = json.loads(_SCALEIN_FILE.read_text(encoding="utf-8")) if _SCALEIN_FILE.exists() else {}
        return data.get(ticker) == str(date.today())
    except Exception:
        return False


def _register_scalein(ticker: str) -> None:
    """Registra el scale-in de hoy. Escritura atómica para evitar race condition."""
    import os, tempfile
    from datetime import date
    try:
        today_str = str(date.today())
        data = json.loads(_SCALEIN_FILE.read_text(encoding="utf-8")) if _SCALEIN_FILE.exists() else {}
        # Limpiar entradas de días anteriores
        data = {k: v for k, v in data.items() if v == today_str}
        data[ticker] = today_str
        tmp_fd, tmp_path = tempfile.mkstemp(dir=_SCALEIN_FILE.parent, prefix=".tmp_", suffix=".json")
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, _SCALEIN_FILE)
    except Exception:
        pass


def _check_scalein_momentum(ticker: str) -> bool:
    """True si MACD bullish y RSI < 73 — el momentum del winner continúa."""
    try:
        import yfinance as yf
        df = yf.download(ticker, period="3mo", interval="1d", progress=False, auto_adjust=True, timeout=10)
        if df is None or len(df) < 30:
            return True  # fail open: sin datos suficientes, asumir ok
        close = df["Close"].squeeze()
        ema12 = close.ewm(span=12).mean()
        ema26 = close.ewm(span=26).mean()
        macd_val   = float(ema12.iloc[-1] - ema26.iloc[-1])
        signal_val = float((ema12 - ema26).ewm(span=9).mean().iloc[-1])
        macd_ok = macd_val > signal_val
        delta = close.diff()
        gain  = delta.clip(lower=0).rolling(14).mean().iloc[-1]
        loss  = (-delta.clip(upper=0)).rolling(14).mean().iloc[-1]
        rsi   = 100 - (100 / (1 + gain / loss)) if loss != 0 else 50.0
        return bool(macd_ok and rsi < 73)
    except Exception:
        return True  # fail open


# PDT guard: Alpaca código 40310100 bloquea el cierre de posiciones equity abiertas
# el mismo día en cuentas < $25k. Registrar el bloqueo para evitar reintentos cada
# 30 min que drenan tokens de Claude y generan órdenes fallidas repetidas.
_PDT_BLOCK_FILE = BASE_DIR / "signals" / "pdt_blocks.json"


def _is_pdt_blocked(ticker: str) -> bool:
    """True si este ticker ya recibió un error PDT hoy — no reintentar hasta mañana."""
    from datetime import date
    try:
        data = json.loads(_PDT_BLOCK_FILE.read_text(encoding="utf-8")) if _PDT_BLOCK_FILE.exists() else {}
        return data.get(ticker) == str(date.today())
    except Exception:
        return False


def _register_pdt_block(ticker: str) -> None:
    """Registra el bloqueo PDT de hoy. Purga entradas de días anteriores para evitar acumulación."""
    from datetime import date
    try:
        today_str = str(date.today())
        data = json.loads(_PDT_BLOCK_FILE.read_text(encoding="utf-8")) if _PDT_BLOCK_FILE.exists() else {}
        # Mantener solo los bloqueos de hoy — los de días anteriores no aplican
        data = {k: v for k, v in data.items() if v == today_str}
        data[ticker] = today_str
        _PDT_BLOCK_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        logger.info("PDT block registrado para %s — se cerrará mañana", ticker)
    except Exception:
        pass


def _build_eod_summary(broker, positions: list, equity: float, capital_base: float) -> str:
    """
    Resumen diario del portfolio enviado al último run del monitor (16:35 ART).
    Incluye P&L del día, comparación vs SPY y estado de cada posición.
    """
    ts = datetime.now().strftime("%d-%b %H:%M")
    lines = [f"📊 *CIERRE DEL DÍA* · {ts}"]

    total_pnl = sum(p.unrealized_pl for p in positions)
    pnl_pct = (total_pnl / capital_base * 100) if capital_base > 0 else 0.0
    pnl_icon = "🟢" if total_pnl >= 0 else "🔴"
    lines.append(f"{pnl_icon} P&L abierto: ${total_pnl:+.2f} ({pnl_pct:+.2f}%)")
    lines.append(f"   Equity: ${equity:.2f} | Capital base: ${capital_base:.0f}")

    # SPY daily change
    try:
        import yfinance as yf
        spy = yf.download("SPY", period="2d", interval="1d", progress=False, auto_adjust=True, timeout=10)
        if spy is not None and len(spy) >= 2:
            spy_close = spy["Close"].squeeze()
            spy_chg = float((spy_close.iloc[-1] - spy_close.iloc[-2]) / spy_close.iloc[-2] * 100)
            lines.append(f"   SPY hoy: {spy_chg:+.2f}% | Alpha vs SPY: {pnl_pct - spy_chg:+.2f}%")
    except Exception:
        pass

    # Posiciones
    if positions:
        lines.append("")
        lines.append("*Posiciones:*")
        for pos in sorted(positions, key=lambda p: p.unrealized_pl, reverse=True):
            pct = pnl_pct_from_position(pos)
            icon = "🟢" if pos.unrealized_pl >= 0 else "🔴"
            lines.append(f"  {icon} {pos.ticker}: ${pos.unrealized_pl:+.2f} ({pct:+.1f}%)")
    else:
        lines.append("Sin posiciones abiertas.")

    return "\n".join(lines)


def _trigger_capital_rotation(closed_ticker: str, pnl_pct: float, live: bool) -> str:
    """
    Después de un TP en CP, corre run_trader.py para re-deployar el capital liberado el mismo día.
    Usa subprocess.run (bloqueante con timeout) para que en Cloud Run el proceso complete antes de salir.
    """
    import subprocess
    gain_str = f"+{pnl_pct:.1f}%" if pnl_pct > 0 else f"{pnl_pct:.1f}%"
    try:
        cmd = [sys.executable, str(BASE_DIR / "run_trader.py")]
        if live:
            cmd.append("--live")
        result = subprocess.run(cmd, cwd=str(BASE_DIR), timeout=240, capture_output=False)
        status = "OK" if result.returncode == 0 else f"rc={result.returncode}"
        logger.info("Capital rotation: %s cerró con %s → trader ejecutado (%s)", closed_ticker, gain_str, status)
        return (
            f"\n🔄 *CAPITAL ROTATION* | {closed_ticker} cerró {gain_str}\n"
            f"  Trader re-ejecutado — capital re-deployed ({status})."
        )
    except Exception as e:
        logger.debug("Capital rotation spawn: %s", e)
        return f"\n🔄 Capital liberado ({closed_ticker} {gain_str}) — midday scan pendiente."


def main():
    parser = argparse.ArgumentParser(description="Monitor intradía de posiciones.")
    parser.add_argument("--live", action="store_true", help="Ejecutar cierres reales en Alpaca.")
    parser.add_argument("--dry-run", action="store_true", help="Loguear sin ejecutar.")
    args = parser.parse_args()

    load_dotenv(BASE_DIR / ".env")
    setup_logging()

    # Lockfile para evitar que dos invocaciones simultáneas pisen la DB y
    # dupliquen llamadas LLM. Cloud Scheduler puede reintentar un Job si
    # excede timeout, y eso podría solapar con la próxima corrida programada.
    lock_path = BASE_DIR / "signals" / ".monitor.lock"
    lock_path.parent.mkdir(exist_ok=True)
    # Timeout 30s: Cloud Run puede sufrir CPU throttle ocasional; 5s era demasiado
    # agresivo. 30s da margen y el siguiente trigger del scheduler está a 30min.
    lock = FileLock(str(lock_path), timeout=30)
    try:
        with lock:
            return _main_locked(args)
    except FileLockTimeout:
        logger.warning("Otro monitor en ejecución (lock %s) — skip.", lock_path)
        return


def _main_locked(args):
    """Cuerpo principal del monitor, ejecutado bajo el filelock."""
    logger.info("=== MONITOR START === live=%s dry_run=%s", args.live, args.dry_run)

    from trader_agent.brokers.alpaca_broker import AlpacaBroker
    from alpha_agent.notifications import send_notification
    from alpha_agent.config import PARAMS

    # ── Watchdog del pipeline (Iter4): detecta stale signals ────────────────────
    # El sistema "no opero del 3 al 18 de mayo sin alerta". Esto previene eso.
    # Si signals/latest.json es de hace >24h, alerta inmediata.
    # Estado guardado en signals/watchdog_state.json para no spammear cada 30min.
    try:
        import json as _wd_json
        from datetime import datetime as _wd_dt, timezone as _wd_tz
        _wd_state_path = BASE_DIR / "signals" / "watchdog_state.json"
        _sig_path = SIGNALS_PATH
        if _sig_path.exists():
            _sig_data = _wd_json.loads(_sig_path.read_text(encoding="utf-8"))
            _gen_str = _sig_data.get("generated_at", "")
            try:
                _gen_dt = _wd_dt.fromisoformat(_gen_str.replace("Z", "+00:00"))
                if _gen_dt.tzinfo is None:
                    _gen_dt = _gen_dt.replace(tzinfo=_wd_tz.utc)
                _age_h = (_wd_dt.now(_wd_tz.utc) - _gen_dt).total_seconds() / 3600
            except Exception:
                _age_h = 999.0  # parsing failed → asumir stale
        else:
            _age_h = 999.0

        # Cooldown: no spam — solo alerta si pasaron >2h desde la ultima alerta del mismo problema
        _last_alert_ts = 0.0
        if _wd_state_path.exists():
            try:
                _last_alert_ts = float(_wd_json.loads(_wd_state_path.read_text()).get("last_alert_ts", 0))
            except Exception:
                pass
        _now_ts = _wd_dt.now(_wd_tz.utc).timestamp()
        _cooldown_ok = (_now_ts - _last_alert_ts) > 7200  # 2h

        if _age_h > 24 and _cooldown_ok:
            _wd_msg = (
                f"🚨 *WATCHDOG* — signals/latest.json STALE\n"
                f"Edad: {_age_h:.0f}h (umbral 24h)\n"
                f"Ultimo analyst: {_gen_str or 'desconocido'}\n"
                f"El sistema NO esta operando. Trigger manual:\n"
                f"`gcloud run jobs execute alpha-daily --region us-central1 --project alpha-agent-2025`"
            )
            logger.warning("WATCHDOG ALERT: signals stale %.0fh", _age_h)
            try:
                send_notification(_wd_msg, header="WATCHDOG")
                _wd_state_path.write_text(_wd_json.dumps({"last_alert_ts": _now_ts}), encoding="utf-8")
            except Exception as _wne:
                logger.debug("watchdog notify fail: %s", _wne)
        elif _age_h > 24:
            logger.warning("WATCHDOG: signals stale %.0fh (alerta en cooldown)", _age_h)
        else:
            logger.info("Watchdog OK: signals %.1fh de edad", _age_h)
    except Exception as _wd_err:
        logger.debug("watchdog error (no critico): %s", _wd_err)

    broker = AlpacaBroker(paper=True)

    # ── 1. Check horario de mercado (salir temprano si el mercado está cerrado)
    try:
        market_open = broker.is_market_open()
    except Exception as e:
        logger.warning("No se pudo verificar horario de mercado: %s. Continuando igual.", e)
        market_open = True  # asumir abierto si falla la consulta

    if not market_open:
        logger.info("Mercado cerrado en este momento. Monitor sale sin acción.")
        return

    # ── 2. Estado actual
    try:
        equity = broker.get_equity()
        positions = broker.get_positions()
    except Exception as e:
        logger.error("Error conectando a Alpaca: %s", e)
        return

    # Escalar equity al espacio virtual ($1600 base)
    try:
        from alpha_agent.analytics.capital_tracker import get_virtual_equity
        equity = get_virtual_equity(equity)
    except Exception:
        pass

    # Sync fills + reconcile DB con Alpaca — mantiene P&L real actualizado
    try:
        from alpha_agent.analytics.trade_db import sync_fills_from_alpaca, reconcile_buy_sell_pairs
        sync_fills_from_alpaca(broker)
        _closed = reconcile_buy_sell_pairs()
        if _closed:
            logger.info("reconcile: %d pares BUY/SELL cerrados automáticamente", _closed)
    except Exception as _re:
        logger.debug("trade_db sync error: %s", _re)

    # Guardar snapshot diario de equity para el gráfico del dashboard
    try:
        import json as _j
        _snap_path = BASE_DIR / "signals" / "equity_snapshots.json"
        _snaps = _j.loads(_snap_path.read_text(encoding="utf-8")) if _snap_path.exists() else []
        _today = datetime.now().strftime("%Y-%m-%d")
        _snaps = [s for s in _snaps if s.get("date") != _today]
        _snaps.append({"date": _today, "equity": round(equity, 2)})
        _snaps = _snaps[-252:]  # keep last trading year
        _snap_path.write_text(_j.dumps(_snaps, indent=2), encoding="utf-8")
    except Exception as _se:
        logger.debug("equity snapshot error: %s", _se)

    logger.info("💰 Equity: $%.2f | Posiciones: %d", equity, len(positions))

    if not positions:
        logger.info("Sin posiciones abiertas. Nada que monitorear.")
        return

    # ── 3. Cargar stops/TPs del último análisis
    signals_data = load_signals_latest()
    capital_base = signals_data.get("capital_usd", PARAMS.paper_capital_usd)
    cp_max_hold_days = _read_cp_max_hold_days()

    # ── 4. Risk budget escalado (Iter3): bandas en lugar de kill binario -3%
    # Reemplaza el kill switch binario por el sistema escalado de kelly.py.
    # Bandas: 0..-2% NORMAL, -2..-4% REDUCE, -4..-6% CLOSE_LOSERS,
    # -6..-8% CLOSE_LONGS, <-8% KILL. Notifica via Telegram en cada cambio.
    drawdown = (capital_base - equity) / capital_base if capital_base > 0 else 0
    drawdown_pct = -drawdown * 100  # negativo para alimentar risk_action_for_drawdown

    alerts: list[str] = []
    closes: list[str] = []

    try:
        from alpha_agent.analytics.kelly import risk_action_for_drawdown
        risk_band = risk_action_for_drawdown(drawdown_pct)
        risk_level = risk_band["level"]
    except Exception as _rb_err:
        logger.debug("risk_action_for_drawdown no disponible (%s) — fallback binario", _rb_err)
        risk_band = {"level": "KILL" if drawdown >= 0.03 else "NORMAL"}
        risk_level = risk_band["level"]

    if risk_level != "NORMAL":
        logger.warning("🚨 RISK BAND %s | DD=%.2f%% | equity=$%.0f baseline=$%.0f",
                       risk_level, drawdown * 100, equity, capital_base)
        alerts.append(
            f"🚨 *RISK BAND {risk_level}*\n"
            f"Equity: ${equity:.0f} | DD {drawdown*100:.1f}% desde ${capital_base:.0f}\n"
            f"{risk_band.get('description', '')}"
        )

    # KILL — cerrar TODO
    if risk_level == "KILL":
        if args.live and not args.dry_run:
            logger.warning("Cerrando TODAS las posiciones...")
            try:
                broker._trading.close_all_positions(cancel_orders=True)
                closes.append("ALL (kill switch)")
                alerts.append("✅ Todas las posiciones cerradas (kill switch).")
            except Exception as e:
                alerts.append(f"❌ Error en close_all_positions: {e}")
                logger.error("Error en kill switch: %s", e)
                logger.warning("Fallback: cerrando posiciones individualmente...")
                for _ks_pos in positions:
                    try:
                        broker._trading.close_position(_ks_pos.ticker, cancel_orders=True)
                        logger.info("Kill switch: %s cerrado OK", _ks_pos.ticker)
                    except Exception as _ks_e:
                        logger.error("Kill switch fallback %s: %s", _ks_pos.ticker, _ks_e)
        else:
            alerts.append("_(dry-run: no se cerraron posiciones)_")
    # CLOSE_LONGS — cerrar longs equity (mantiene hedge / opciones)
    elif risk_level == "CLOSE_LONGS":
        if args.live and not args.dry_run:
            logger.warning("CLOSE_LONGS — cerrando posiciones equity long, manteniendo hedge")
            for _ks_pos in positions:
                _is_opt = getattr(_ks_pos, "asset_class", "equity") == "option"
                if _is_opt:
                    continue  # mantener hedge / opciones
                _is_long = float(getattr(_ks_pos, "qty", 0)) > 0
                if _is_long:
                    try:
                        broker._trading.close_position(_ks_pos.ticker, cancel_orders=True)
                        closes.append(f"{_ks_pos.ticker} (CLOSE_LONGS)")
                        logger.info("CLOSE_LONGS: %s cerrado", _ks_pos.ticker)
                    except Exception as _ke:
                        logger.error("CLOSE_LONGS fail %s: %s", _ks_pos.ticker, _ke)
        else:
            alerts.append("_(dry-run: no se cerraron longs)_")
    # CLOSE_LOSERS — cerrar sólo perdedores (P&L < 0)
    elif risk_level == "CLOSE_LOSERS":
        if args.live and not args.dry_run:
            logger.warning("CLOSE_LOSERS — cerrando posiciones con P&L negativo")
            for _ks_pos in positions:
                try:
                    if float(getattr(_ks_pos, "unrealized_pl", 0)) >= 0:
                        continue
                    broker._trading.close_position(_ks_pos.ticker, cancel_orders=True)
                    closes.append(f"{_ks_pos.ticker} (CLOSE_LOSERS, P&L<0)")
                    logger.info("CLOSE_LOSERS: %s cerrado", _ks_pos.ticker)
                except Exception as _ke:
                    logger.error("CLOSE_LOSERS fail %s: %s", _ks_pos.ticker, _ke)
        else:
            alerts.append("_(dry-run: no se cerraron losers)_")
    # REDUCE — no cerramos pero marcamos el flag para que el próximo daily no entre
    elif risk_level == "REDUCE":
        try:
            from pathlib import Path as _P
            (_P(__file__).parent / "signals" / "reduce_mode.flag").write_text(
                f"DD {drawdown*100:.1f}% — REDUCE mode activo. Eliminar este archivo para resetear.",
                encoding="utf-8",
            )
            logger.info("REDUCE flag escrito: signals/reduce_mode.flag")
        except Exception:
            pass

    else:
        # ── 5b. VIX SPIKE PROTOCOL: reducir CP al 50% si VIX sube >20% intradía
        vix_spike, vix_now, vix_prev = _check_vix_spike()
        if vix_spike:
            logger.warning("⚡ VIX SPIKE: %.1f → %.1f (+%.0f%%) — reduciendo CP al 50%%",
                           vix_prev, vix_now, (vix_now/vix_prev - 1)*100)
            alerts.append(
                f"⚡ *VIX SPIKE* {vix_prev} → {vix_now} (+{(vix_now/vix_prev-1)*100:.0f}%)\n"
                f"  Reduciendo posiciones CP al 50%% como protección de capital."
            )
            for pos in positions:
                sig = get_signal_for_ticker(signals_data, pos.ticker)
                if not sig:
                    continue
                if (sig.get("horizon") or "").upper() not in ("CP", "MIX"):
                    continue
                sell_qty = round(abs(pos.qty) / 2, 4)
                if sell_qty <= 0:
                    continue
                logger.info("VIX SPIKE REDUCE %s: vendiendo %.4f shares", pos.ticker, sell_qty)
                alerts.append(f"  📉 REDUCE {pos.ticker}: vendiendo 50%% ({sell_qty:.4f} shares)")
                if args.live and not args.dry_run:
                    try:
                        from alpaca.trading.requests import MarketOrderRequest
                        from alpaca.trading.enums import OrderSide, TimeInForce
                        order = MarketOrderRequest(
                            symbol=pos.ticker, qty=sell_qty,
                            side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
                        )
                        broker._trading.submit_order(order)
                        closes.append(f"{pos.ticker} (VIX spike 50%)")
                    except Exception as _vix_e:
                        logger.error("VIX spike reduce %s: %s", pos.ticker, _vix_e)
                else:
                    alerts.append("    (dry-run: no enviado)")

        # ── 5c. EOD DT close: cerrar posiciones DT antes del cierre de mercado
        # NYSE cierra 16:00 EDT. Forzamos cierre DT a las 15:00 EDT = 19:00 UTC
        # para asegurarnos de no tener overnight en el sleeve de day trading.
        from datetime import timezone as _tz
        _now_utc = datetime.now(_tz.utc)
        if _now_utc.hour >= 19:
            try:
                from alpha_agent.analytics.trade_db import get_open_dt_tickers
                dt_open = get_open_dt_tickers()
                if dt_open:
                    logger.info("EOD DT close: cerrando %d posicion(es) DT %s", len(dt_open), sorted(dt_open))
                    # Usar el broker DT separado — no el broker LP/CP del monitor
                    _dt_key = os.getenv("ALPACA_DT_API_KEY")
                    _dt_sec = os.getenv("ALPACA_DT_SECRET_KEY")
                    dt_positions_to_close: list = []
                    dt_broker = None
                    if _dt_key and _dt_sec:
                        _prev_key = os.environ.get("ALPACA_API_KEY", "")
                        _prev_sec = os.environ.get("ALPACA_SECRET_KEY", "")
                        os.environ["ALPACA_API_KEY"]    = _dt_key
                        os.environ["ALPACA_SECRET_KEY"] = _dt_sec
                        try:
                            from trader_agent.brokers.alpaca_broker import AlpacaBroker as _ABroker
                            dt_broker = _ABroker(paper=True)
                            dt_positions_to_close = [p for p in dt_broker.get_positions()
                                                     if p.ticker in dt_open]
                        except Exception as _be:
                            logger.warning("DT broker EOD: %s", _be)
                        finally:
                            os.environ["ALPACA_API_KEY"]    = _prev_key
                            os.environ["ALPACA_SECRET_KEY"] = _prev_sec
                    for pos in dt_positions_to_close:
                        current_dt  = current_price_from_position(pos)
                        pnl_dt      = pos.unrealized_pl
                        pnl_pct_dt  = pnl_pct_from_position(pos)
                        logger.info("EOD DT: cerrando %s | P&L $%+.2f (%+.1f%%)",
                                    pos.ticker, pnl_dt, pnl_pct_dt)
                        alerts.append(
                            f"EOD DT CLOSE *{pos.ticker}* | P&L ${pnl_dt:+.2f} ({pnl_pct_dt:+.1f}%)"
                        )
                        if args.live and not args.dry_run and dt_broker:
                            try:
                                from alpaca.trading.requests import MarketOrderRequest
                                from alpaca.trading.enums import OrderSide, TimeInForce
                                order = MarketOrderRequest(
                                    symbol=pos.ticker,
                                    qty=abs(pos.qty),
                                    side=OrderSide.SELL,
                                    time_in_force=TimeInForce.DAY,
                                )
                                dt_broker._trading.submit_order(order)
                                closes.append(f"{pos.ticker} (EOD-DT)")
                                alerts.append(f"  SELL {abs(pos.qty):.4f} {pos.ticker} enviado (DT)")
                                from alpha_agent.analytics.trade_db import log_trade_close
                                log_trade_close(
                                    ticker=pos.ticker,
                                    exit_price=round(current_dt, 2),
                                    pnl_usd=round(pnl_dt, 2),
                                    pnl_pct=round(pnl_pct_dt, 2),
                                )
                            except Exception as _eod_e:
                                logger.error("EOD DT close %s: %s", pos.ticker, _eod_e)
                                alerts.append(f"  ERROR cerrando {pos.ticker}: {_eod_e}")
                        else:
                            alerts.append("  (dry-run: no enviado)")
            except Exception as _eod_err:
                logger.debug("EOD DT check: %s", _eod_err)

        # ── 6. POR POSICIÓN: check stops, TPs, trailing
        for pos in positions:
            ticker = pos.ticker
            avg_entry = pos.avg_price
            qty = pos.qty
            current = current_price_from_position(pos)
            unrealized_pnl = pos.unrealized_pl
            pnl_pct = pnl_pct_from_position(pos)

            signal = get_signal_for_ticker(signals_data, ticker)
            if not signal:
                # Sin señal en latest.json: posición huérfana (LP o DT abierta sin signal activo).
                # Aplicar stop de emergencia: -12% LP / -8% DT para evitar pérdidas ilimitadas.
                _orphan_stop_pct = 0.08 if pos.ticker in getattr(pos, "sleeve", "") else 0.12
                _orphan_threshold = avg_entry * (1 - _orphan_stop_pct) if avg_entry else 0
                if _orphan_threshold and current <= _orphan_threshold:
                    logger.warning(
                        "⚠️ ORPHAN STOP %s: sin señal + caída -%.0f%% ($%.2f <= $%.2f)",
                        ticker, _orphan_stop_pct * 100, current, _orphan_threshold,
                    )
                    alerts.append(
                        f"⚠️ *{ticker}*: posición sin señal activa — stop de emergencia "
                        f"(-{_orphan_stop_pct*100:.0f}%) disparado (${current:.2f})"
                    )
                    if args.live and not args.dry_run:
                        try:
                            broker.close_position(ticker)
                        except Exception as _oe:
                            logger.error("Error cerrando posición huérfana %s: %s", ticker, _oe)
                else:
                    logger.warning("Posición huérfana %s (sin señal en latest.json) — P&L %.1f%%", ticker, pnl_pct)
                continue

            stop_loss = signal.get("stop_loss")
            take_profit = signal.get("take_profit")
            macro_regime = signals_data.get("macro", {}).get("regime", "unknown")

            # Sanidad: para long, TP debe estar SOBRE la entrada. Si TP < avg_entry,
            # la señal es stale (fue generada cuando el precio era más bajo).
            # En ese caso nulificamos el TP y dejamos solo stop/chandelier.
            if take_profit and avg_entry and float(take_profit) < float(avg_entry) * 0.99:
                logger.warning(
                    "%s: TP stale (${:.2f}) < avg_entry (${:.2f}) — usando solo stop/chandelier".format(take_profit, avg_entry),
                    ticker,
                )
                take_profit = None

            action = None
            reason = ""
            claude_override = False
            near_tp = False
            is_cp_expire = False  # True → rotación forzada por tiempo, no vetoable por Claude

            # ── CP max hold days: rotación forzada por tiempo ────────────────
            horizon = (signal.get("horizon") or "").upper()
            if horizon in ("CP", "MIX"):
                entry_date_str = _get_cp_entry_date(ticker)
                if not entry_date_str:
                    # Fallback: intentar fecha de apertura del broker (Alpaca position.created_at)
                    try:
                        _ap = broker._trading.get_open_position(ticker)
                        _ca = getattr(_ap, "created_at", None) or getattr(_ap, "asset_change_at", None)
                        if _ca:
                            entry_date_str = str(_ca)[:10]
                            logger.debug("CP %s: usando created_at del broker como fecha de entrada (%s)", ticker, entry_date_str)
                    except Exception:
                        pass
                if entry_date_str:
                    try:
                        from datetime import date as _date
                        entry_date = _date.fromisoformat(entry_date_str[:10])
                        held_days  = (_date.today() - entry_date).days
                        if held_days >= cp_max_hold_days:
                            action = "CLOSE"
                            is_cp_expire = True
                            reason = (
                                f"CP MAX HOLD DAYS alcanzado ({held_days}d >= {cp_max_hold_days}d) "
                                f"— rotación forzada"
                            )
                            logger.info(
                                "CP EXPIRE %s: %d días >= max %d → cerrando",
                                ticker, held_days, cp_max_hold_days,
                            )
                    except Exception:
                        pass
                else:
                    logger.warning("CP %s: sin fecha de entrada en trade_db ni broker — max hold no verificado", ticker)

            # Check stop loss
            stop_loss_hit = bool(stop_loss and current <= stop_loss)
            if stop_loss_hit:
                action = "CLOSE"
                reason = f"STOP LOSS tocado (${current:.2f} <= SL ${stop_loss:.2f})"

            # Check take profit
            elif take_profit and current >= take_profit:
                action = "CLOSE"
                reason = f"TAKE PROFIT alcanzado (${current:.2f} >= TP ${take_profit:.2f})"

            # ── Partial TP: vende 50% al 93% del TP, deja correr el resto ───
            elif (
                take_profit is not None
                and current >= take_profit * 0.93
                and not _was_partially_exited_today(ticker)
            ):
                action = "REDUCE"
                near_tp = True
                reason = (
                    f"PARTIAL TP (${current:.2f} ≈ ${take_profit:.2f}) "
                    f"— vende 50%, resta corre libre de riesgo"
                )

            # ── Adaptive trailing por conviction × régimen (Iter2) ───────────
            # Antes era un breakeven fijo +8%. Ahora la tabla en kelly.adaptive_trailing
            # modula según conviction + régimen:
            #   BULL+ALTA:  BE a +8%, lock 60% del profit a +20%  ← deja correr
            #   BEAR+MEDIA: BE a +2%, lock 30% del profit a +5%   ← protege rápido
            if action is None and avg_entry > 0 and pnl_pct > 0:
                try:
                    from alpha_agent.analytics.kelly import adaptive_trailing
                    _conv = (signal.get("thesis", {}) or {}).get("conviction", "MEDIA")
                    _trail = adaptive_trailing(_conv, macro_regime)
                    current_sl = signal.get("stop_loss") or 0.0

                    # Si el profit alcanzó la banda de lock-profit, mover stop a
                    # entrada + fracción del profit. Si no, intentar breakeven.
                    if pnl_pct >= _trail["lock_at_pct"]:
                        lock_sl = round(
                            avg_entry * (1.0 + (pnl_pct / 100.0) * _trail["lock_fraction"]),
                            2,
                        )
                        if lock_sl > current_sl + 0.01:
                            _update_trailing_stop(
                                broker, ticker, lock_sl, abs(qty),
                                args.live and not args.dry_run, alerts,
                                f"LOCK PROFIT ({_conv}/{macro_regime}) +{pnl_pct:.1f}% → SL ${lock_sl:.2f} "
                                f"(protege {_trail['lock_fraction']*100:.0f}% del profit)",
                            )
                            signal["stop_loss"] = lock_sl
                    elif pnl_pct >= _trail["be_at_pct"]:
                        breakeven_sl = round(avg_entry * 1.005, 2)
                        if breakeven_sl > current_sl + 0.01:
                            _update_trailing_stop(
                                broker, ticker, breakeven_sl, abs(qty),
                                args.live and not args.dry_run, alerts,
                                f"BREAKEVEN ({_conv}/{macro_regime}) +{pnl_pct:.1f}% → SL ${breakeven_sl:.2f}",
                            )
                            signal["stop_loss"] = breakeven_sl
                except Exception as _at_err:
                    # Fallback al breakeven fijo si algo falla con la tabla nueva
                    logger.debug("adaptive_trailing falló (%s) — fallback BE +8%%", _at_err)
                    if pnl_pct >= 8.0:
                        breakeven_sl = round(avg_entry * 1.005, 2)
                        current_sl = signal.get("stop_loss") or 0.0
                        if breakeven_sl > current_sl + 0.01:
                            _update_trailing_stop(
                                broker, ticker, breakeven_sl, abs(qty),
                                args.live and not args.dry_run, alerts,
                                f"BREAKEVEN (fallback) +{pnl_pct:.1f}% → SL ${breakeven_sl:.2f}",
                            )
                            signal["stop_loss"] = breakeven_sl

            # Chandelier Exit trailing stop (Chuck LeBeau)
            # Sube dinámicamente con el precio; solo cierra si el precio cae
            if action is None and avg_entry > 0:
                chandelier_level = _compute_chandelier_stop(ticker, macro_regime)
                current_sl = signal.get("stop_loss") or 0.0

                if chandelier_level is not None and chandelier_level > current_sl + 0.01:
                    _update_trailing_stop(
                        broker, ticker, chandelier_level, abs(qty), args.live and not args.dry_run,
                        alerts,
                        f"Chandelier Exit @ ${chandelier_level:.2f} (era SL ${current_sl:.2f})",
                    )
                    signal["stop_loss"] = chandelier_level

                # Si el precio actual cae por debajo del chandelier → cerrar
                effective_sl = signal.get("stop_loss")
                if effective_sl and current <= effective_sl:
                    gain_pct = (current - avg_entry) / avg_entry
                    action = "CLOSE"
                    reason = f"CHANDELIER EXIT TOCADO (${current:.2f} <= ${effective_sl:.2f}, P&L {gain_pct*100:+.1f}%)"

            # ── Claude intelligence: consultar cuando estamos cerca del stop ──
            # Si el precio está dentro del 1.5% del stop loss (pero no lo tocó),
            # o si el stop fue tocado, pedimos a Claude que evalúe con contexto noticioso.
            near_stop = (
                stop_loss is not None
                and current > stop_loss
                and (current - stop_loss) / stop_loss < 0.015
            )

            # PDT guard: si el ticker está bloqueado hoy, no intentar cerrar ni llamar a Claude
            if action in ("CLOSE", "REDUCE") and _is_pdt_blocked(ticker):
                logger.info("PDT block activo para %s — saltando cierre hasta mañana", ticker)
                alerts.append(
                    f"  🔒 *{ticker}*: PDT protection activa — posición abierta hoy, "
                    f"se cerrará mañana (P&L actual ${unrealized_pnl:+.2f})"
                )
                action = None

            if (action == "CLOSE" or near_stop) and not claude_override and not is_cp_expire:
                # Cooldown activo: el veto sigue vigente → ejecutar sin re-consultar.
                # Post-veto (cooldown expirado pero fue hoy): un veto por día por posición
                # → ejecutar directamente para cortar el loop veto→3h→veto→3h.
                if _is_in_veto_cooldown(ticker):
                    logger.info(
                        "🤖 %s en cooldown de veto Claude — ejecutando sin consulta", ticker
                    )
                elif _is_post_veto_today(ticker):
                    logger.info(
                        "🤖 %s ya vetado hoy — ejecutando directamente (1 veto/día por posición)", ticker
                    )
                else:
                    news_for_ticker: list[str] = []
                    try:
                        thesis = signal.get("thesis", {})
                        news_for_ticker = thesis.get("news", {}).get("headlines", [])
                        if not news_for_ticker:
                            news_for_ticker = []
                    except Exception:
                        pass

                    claude_result = claude_assess(
                        ticker=ticker,
                        current_price=current,
                        entry_price=avg_entry,
                        pnl_pct=pnl_pct,
                        stop_loss=stop_loss,
                        news_headlines=news_for_ticker,
                        macro_regime=macro_regime,
                    )

                    if claude_result:
                        claude_action = claude_result["action"]
                        claude_reason = claude_result["reason"]
                        claude_conf = claude_result["confidence"]
                        logger.info(
                            "🤖 Claude sobre %s: %s (conf=%.0f%%) — %s",
                            ticker, claude_action, claude_conf * 100, claude_reason,
                        )

                        if near_stop and action is None:
                            # Stop no tocado pero cerca: Claude puede recomendar cerrar anticipadamente
                            if claude_action == "CLOSE" and claude_conf >= 0.75:
                                action = "CLOSE"
                                reason = f"CIERRE ANTICIPADO por Claude: {claude_reason}"
                                claude_override = True
                            elif claude_action == "REDUCE" and claude_conf >= 0.70:
                                action = "REDUCE"
                                reason = f"REDUCCIÓN por Claude: {claude_reason}"
                                claude_override = True
                        elif action == "CLOSE":
                            if stop_loss_hit:
                                # Stop duro tocado: regla de riesgo inviolable — Claude NO puede vetar
                                if claude_action == "HOLD":
                                    logger.info(
                                        "🤖 Claude recomendaba HOLD para %s pero el stop duro es inviolable — cerrando",
                                        ticker,
                                    )
                                    alerts.append(
                                        f"🤖 *{ticker}*: stop alcanzado — cerrando "
                                        f"(Claude sugería HOLD: {claude_reason})"
                                    )
                                # action permanece "CLOSE" — sin veto posible
                            else:
                                # TP o trailing stop: Claude puede vetar UNA VEZ (3h cooldown)
                                if claude_action == "HOLD" and claude_conf >= 0.80:
                                    logger.warning(
                                        "🤖 Claude veta TP/trailing de %s (conf=%.0f%%) — %s",
                                        ticker, claude_conf * 100, claude_reason,
                                    )
                                    alerts.append(
                                        f"🤖 *{ticker}*: TP/trailing tocado, Claude recomienda HOLD "
                                        f"(conf={claude_conf:.0%}) — {claude_reason} "
                                        f"[cooldown {_VETO_COOLDOWN_HOURS}h activo]"
                                    )
                                    _register_veto(ticker, claude_reason)
                                    action = None  # veta solo TP/trailing, nunca el stop duro

            if action in ("CLOSE", "REDUCE"):
                log_msg = f"⚠️ {ticker}: {reason} | P&L: ${unrealized_pnl:+.2f} ({pnl_pct:+.1f}%)"
                logger.warning(log_msg)
                alerts.append(f"⚠️ *{ticker}*: {reason} | P&L ${unrealized_pnl:+.2f}")

                # Flag si el cierre es por TP (capital liberado listo para rotar)
                is_tp_close = action == "CLOSE" and take_profit and current >= take_profit

                if args.live and not args.dry_run:
                    try:
                        # Cancelar stop orders activos para liberar held_for_orders
                        try:
                            for _o in broker.get_open_orders(ticker):
                                if "stop" in str(getattr(_o, "order_type", "")).lower():
                                    broker._trading.cancel_order_by_id(str(_o.id))
                                    alerts.append(f"  🗑️ Stop cancelado ({ticker})")
                        except Exception as _ce:
                            alerts.append(f"  ⚠️ cancel stop {ticker}: {_ce}")
                        from alpaca.trading.requests import MarketOrderRequest
                        from alpaca.trading.enums import OrderSide, TimeInForce
                        sell_qty = abs(qty) if action == "CLOSE" else abs(qty) / 2
                        order = MarketOrderRequest(
                            symbol=ticker,
                            qty=sell_qty,
                            side=OrderSide.SELL,
                            time_in_force=TimeInForce.DAY,
                        )
                        broker._trading.submit_order(order)
                        label = "SELL" if action == "CLOSE" else "SELL HALF"
                        closes.append(f"{ticker} ({reason[:30]})")
                        alerts.append(f"  ✅ {label} enviado: {sell_qty:.4f} {ticker}")

                        # Registrar cierre en SQLite para P&L real
                        try:
                            from alpha_agent.analytics.trade_db import log_trade_close
                            closed_pnl = unrealized_pnl if action == "CLOSE" else unrealized_pnl / 2
                            closed_pct = pnl_pct if action == "CLOSE" else pnl_pct / 2
                            log_trade_close(
                                ticker=ticker,
                                exit_price=current,
                                pnl_usd=round(closed_pnl, 2),
                                pnl_pct=round(closed_pct, 2),
                            )
                        except Exception as _db_e:
                            logger.debug("trade_db close log error: %s", _db_e)

                        # Partial TP: registrar la venta parcial como PARTIAL_TP
                        # para que _was_partially_exited_today() la detecte y evite doble venta
                        if near_tp:
                            _log_partial_exit(
                                ticker, sell_qty, current, round(unrealized_pnl / 2, 2)
                            )

                        # Capital rotation: lanza midday scan en background para reutilizar capital
                        if is_tp_close:
                            _rotation_alert = _trigger_capital_rotation(
                                ticker, pnl_pct, args.live and not args.dry_run
                            )
                            alerts.append(_rotation_alert)
                    except Exception as e:
                        err_str = str(e)
                        alerts.append(f"  ❌ Error cerrando {ticker}: {e}")
                        logger.error("Error cerrando %s: %s", ticker, e)
                        if "40310100" in err_str or "pattern day" in err_str.lower():
                            _register_pdt_block(ticker)
                            alerts.append(
                                f"  🔒 PDT protection — {ticker} abierta hoy, "
                                f"se cerrará en el próximo ciclo mañana"
                            )
                else:
                    alerts.append("  _(dry-run: orden no enviada)_")

            else:
                # Posición OK
                logger.info(
                    "✅ %s: $%.2f (entry $%.2f) | P&L $%+.2f (%+.1f%%) — OK",
                    ticker, current, avg_entry, unrealized_pnl, pnl_pct,
                )

                # ── Scale-in: agregar capital cuando el winner sigue con momentum ──
                # Condiciones: CP/MIX, PnL >= 12%, no escalado hoy, MACD+RSI ok
                if (
                    horizon in ("CP", "MIX")
                    and pnl_pct >= 12.0
                    and avg_entry > 0
                    and not _was_scaled_in_today(ticker)
                ):
                    if _check_scalein_momentum(ticker):
                        _si_notional = round(min(abs(qty) * avg_entry * 0.15, 250.0), 2)
                        try:
                            _bp = broker.get_buying_power()
                        except Exception:
                            _bp = 0.0
                        if _si_notional >= 50.0 and _bp >= _si_notional * 1.2:
                            _si_qty = round(_si_notional / current, 4)
                            if args.live and not args.dry_run:
                                try:
                                    from alpaca.trading.requests import MarketOrderRequest
                                    from alpaca.trading.enums import OrderSide, TimeInForce
                                    _si_order = MarketOrderRequest(
                                        symbol=ticker,
                                        qty=_si_qty,
                                        side=OrderSide.BUY,
                                        time_in_force=TimeInForce.DAY,
                                    )
                                    broker._trading.submit_order(_si_order)
                                    _register_scalein(ticker)
                                    alerts.append(
                                        f"📈 *SCALE-IN {ticker}*: +${_si_notional:.0f} "
                                        f"({_si_qty:.4f} sh) | P&L +{pnl_pct:.1f}% | MACD+RSI ok"
                                    )
                                    logger.info(
                                        "SCALE-IN %s: +$%.0f (%.4f sh, PnL=+%.1f%%)",
                                        ticker, _si_notional, _si_qty, pnl_pct,
                                    )
                                except Exception as _sie:
                                    logger.warning("scale-in %s fallido: %s", ticker, _sie)
                            else:
                                alerts.append(
                                    f"📈 _(dry-run)_ SCALE-IN {ticker}: +${_si_notional:.0f} "
                                    f"| P&L +{pnl_pct:.1f}%"
                                )

        # Persistir stop_loss actualizados por chandelier/breakeven de vuelta al JSON.
        # Sin esto, el próximo run re-lee el stop original y re-envía la misma orden a Alpaca.
        # Atomic write: si Cloud Run mata el container a mitad de escribir, el archivo
        # original queda intacto y la próxima corrida no lee basura.
        try:
            import os as _os
            _tmp = SIGNALS_PATH.with_suffix(".json.tmp")
            _tmp.write_text(
                __import__("json").dumps(signals_data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            _os.replace(_tmp, SIGNALS_PATH)
        except Exception as _sp_err:
            logger.debug("No se pudo persistir signals: %s", _sp_err)

        # ── 6b. OPTIONS MONITORING ────────────────────────────────────────────
        # Las opciones no tienen stop-loss ni TP en Alpaca; se gestionan acá.
        # Reglas: cerrar si (a) prima perdida ≥50%, (b) ≤5d al vencimiento, (c) +100% ganancia.
        from datetime import date as _opt_date
        _today = _opt_date.today()
        _opt_positions = [p for p in positions if getattr(p, "asset_class", "equity") == "option"]

        for _op in _opt_positions:
            _sym  = _op.ticker
            _meta = _parse_option_symbol(_sym)
            if not _meta:
                logger.debug("Símbolo de opción no parseable: %s", _sym)
                continue

            _contracts  = max(1, int(abs(_op.qty)))
            _cost_basis = _op.avg_price * _contracts * 100
            _dte        = (_meta["expiry"] - _today).days
            _pnl_pct_op = (_op.unrealized_pl / _cost_basis * 100) if _cost_basis > 0 else 0.0

            _opt_action = None
            _opt_reason = ""

            # PDT guard: Alpaca bloquea ventas el mismo día de la compra en cuentas < $25k.
            # Regla: solo intentar cerrar opciones compradas en días anteriores.
            _opt_entry_date: str | None = None
            try:
                from alpha_agent.analytics.trade_db import get_trades
                for _t in get_trades(ticker=_sym, limit=10):
                    if _t.get("side") == "BUY" and _t.get("closed_at") is None:
                        _opt_entry_date = _t.get("date")
                        break
            except Exception:
                pass
            _opened_today = (_opt_entry_date == _today.isoformat()) if _opt_entry_date else False

            if _pnl_pct_op <= -50:
                if _opened_today:
                    logger.info(
                        "OPT %s: -50%% pero abierta HOY — skip (PDT protection)", _sym
                    )
                elif _dte <= 1:
                    # Prima casi cero + vence mañana: vender no recupera nada y puede
                    # fallar por PDT. Dejar que expire sin alertar.
                    logger.info(
                        "OPT %s: -50%% + DTE=1 — dejando expirar mañana (prima residual ~$0)",
                        _sym,
                    )
                else:
                    _opt_action = "CLOSE"
                    _opt_reason = f"PRIMA -50% ({_pnl_pct_op:.0f}%) — stop-loss de opciones"
            elif _dte <= 5:
                if _opened_today or _dte <= 1:
                    logger.info(
                        "OPT %s: DTE=%d — %s — skip",
                        _sym, _dte,
                        "PDT (abierta hoy)" if _opened_today else "expira mañana, sin acción",
                    )
                else:
                    _opt_action = "CLOSE"
                    _opt_reason = f"NEAR EXPIRY: {_dte}d al vencimiento — theta acelerado"
            elif _pnl_pct_op >= 100:
                _opt_action = "CLOSE"
                _opt_reason = f"TAKE PROFIT: +{_pnl_pct_op:.0f}% (2× prima)"

            logger.info(
                "%s OPT %s [%s %s K=%.0f exp=%s] | val=$%.0f pnl=%+.0f%% dte=%dd",
                "⚠️" if _opt_action else "✅",
                _sym, _meta["type"].upper(), _meta["underlying"],
                _meta["strike"], _meta["expiry"],
                _op.market_value, _pnl_pct_op, _dte,
            )

            if _opt_action:
                alerts.append(
                    f"🎯 *OPT {_meta['underlying']} {_meta['type'].upper()} "
                    f"K${_meta['strike']:.0f}*: {_opt_reason} "
                    f"| P&L ${_op.unrealized_pl:+.0f} ({_pnl_pct_op:+.0f}%)"
                )
                if args.live and not args.dry_run:
                    try:
                        from alpaca.trading.requests import MarketOrderRequest
                        from alpaca.trading.enums import OrderSide, TimeInForce
                        _opt_order = MarketOrderRequest(
                            symbol=_sym,
                            qty=_contracts,
                            side=OrderSide.SELL,
                            time_in_force=TimeInForce.DAY,
                        )
                        broker._trading.submit_order(_opt_order)
                        closes.append(f"{_sym} ({_opt_reason[:20]})")
                        alerts.append(f"  ✅ SELL {_contracts} contrato(s) enviado")
                    except Exception as _oe:
                        _oe_str = str(_oe)
                        # PDT error (40310100): Alpaca bloquea venta de mismo instrumento
                        # comprado hoy. Si DTE ≤ 1, la opción expirará sola — no alertar.
                        if "40310100" in _oe_str and _dte <= 1:
                            logger.info(
                                "OPT %s: PDT error + DTE=1 — expirará mañana, sin alerta", _sym
                            )
                        else:
                            logger.error("Error cerrando opción %s: %s", _sym, _oe)
                            alerts.append(f"  ❌ Error: {_oe}")
                else:
                    alerts.append("  _(dry-run: no enviado)_")

        # ── 6c. DT SAFETY NET: si el bracket de Alpaca falló, el monitor cierra ──
        # Alpaca bracket es muy confiable, pero ante un error de API o slippage extremo
        # esta sección actúa como último recurso. Solo se activa si P&L < -2.5%
        # (0.5% de margen sobre el SL del bracket de -1.5% → evita falsos positivos).
        try:
            from alpha_agent.analytics.trade_db import get_open_dt_tickers
            _dt_open = get_open_dt_tickers()
            if _dt_open:
                _DT_SAFETY_THRESHOLD = -2.5  # % — margen sobre SL bracket (-1.5%)
                for _dp in positions:
                    if _dp.ticker not in _dt_open:
                        continue
                    if getattr(_dp, "asset_class", "equity") != "equity":
                        continue
                    _dt_pnl_pct = pnl_pct_from_position(_dp)
                    if _dt_pnl_pct < _DT_SAFETY_THRESHOLD:
                        _dt_reason = (
                            f"DT SAFETY NET: P&L {_dt_pnl_pct:.1f}% "
                            f"(bracket posiblemente falló — cerrando)"
                        )
                        logger.warning("⚠️ %s: %s", _dp.ticker, _dt_reason)
                        alerts.append(
                            f"🚨 *{_dp.ticker}* {_dt_reason} "
                            f"| ${_dp.unrealized_pl:+.0f}"
                        )
                        if args.live and not args.dry_run:
                            try:
                                from alpaca.trading.requests import MarketOrderRequest
                                from alpaca.trading.enums import OrderSide, TimeInForce
                                _dt_close = MarketOrderRequest(
                                    symbol=_dp.ticker,
                                    qty=abs(_dp.qty),
                                    side=OrderSide.SELL,
                                    time_in_force=TimeInForce.DAY,
                                )
                                broker._trading.submit_order(_dt_close)
                                closes.append(f"{_dp.ticker} (DT safety)")
                                alerts.append(f"  ✅ SELL {abs(_dp.qty):.0f} enviado")
                                from alpha_agent.analytics.trade_db import log_trade_close
                                log_trade_close(
                                    ticker=_dp.ticker,
                                    exit_price=round(current_price_from_position(_dp), 2),
                                    pnl_usd=round(_dp.unrealized_pl, 2),
                                    pnl_pct=round(_dt_pnl_pct, 2),
                                )
                            except Exception as _dt_e:
                                logger.error("DT safety close %s: %s", _dp.ticker, _dt_e)
                                alerts.append(f"  ❌ Error: {_dt_e}")
                        else:
                            alerts.append("  _(dry-run: no enviado)_")
        except Exception as _dt_err:
            logger.debug("DT safety check: %s", _dt_err)

    # ── 7. EOD SUMMARY: último run del día (17:05 ART = 20:05 UTC)
    # ART = UTC-3. NYSE cierra 20:00 UTC. El monitor de las 17:05 ART = 20:05 UTC
    # es el único en el rango 20:00-20:14 → dispara exactamente 1 vez por día.
    from datetime import timezone as _tz2
    _now_utc2 = datetime.now(_tz2.utc)
    if _now_utc2.hour == 20 and _now_utc2.minute < 15:
        try:
            eod_msg = _build_eod_summary(broker, positions, equity, capital_base)
            logger.info("Enviando EOD summary...")
            send_notification(eod_msg)
        except Exception as _eod_sum_err:
            logger.warning("EOD summary error: %s", _eod_sum_err)

    # ── 8. REPORTE de alertas
    if alerts:
        now = datetime.now().strftime("%H:%M")
        header = f"🔔 *MONITOR* · {now} · ${equity:.0f}"
        body = "\n".join(alerts)
        footer_parts = [f"\nPosiciones abiertas: {len(positions)}"]
        if closes:
            footer_parts.append(f"Cerradas: {', '.join(closes)}")
        footer = "\n".join(footer_parts)

        msg = f"{header}\n{body}{footer}"
        logger.info("Enviando alerta (%d chars)...", len(msg))
        try:
            send_notification(msg)
        except Exception as e:
            logger.error("Error enviando notificacion: %s", e)
    else:
        logger.info("📊 Todas las posiciones dentro de rango. Sin alertas.")

    logger.info("=== MONITOR OK ===")


if __name__ == "__main__":
    main()
