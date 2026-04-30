"""
Midday CP scan — 14:00 ART (11:00 EDT).

Escanea técnicamente el universo completo (sin CAPM/Markowitz pesado)
y abre nuevas posiciones CP si hay capital disponible y momentum claro.
Solo toca el sleeve CP; las posiciones LP del analyst no se modifican.
Duración estimada: ~3 min.

Uso:
    python run_midday.py          # dry-run
    python run_midday.py --live   # ejecuta órdenes reales
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.platform == "win32" and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import argparse
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent.resolve()
SIGNALS_PATH = BASE_DIR / "signals" / "latest.json"

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s — %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("midday")


def _score_ticker(close, high, low, volume) -> dict:
    """Calcula score técnico compuesto para un ticker. Retorna None si datos insuficientes."""
    import numpy as np

    if len(close) < 50:
        return None

    # RSI(14)
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    rs    = gain / loss.replace(0, float("nan"))
    rsi   = float((100 - 100 / (1 + rs)).iloc[-1])
    if np.isnan(rsi):
        return None

    # MACD(12,26,9)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd  = ema12 - ema26
    sig   = macd.ewm(span=9, adjust=False).mean()
    macd_bull = int(float(macd.iloc[-1]) > float(sig.iloc[-1]) and float((macd - sig).iloc[-1]) > 0)

    # EMA20 / EMA50
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    above_ema20 = int(float(close.iloc[-1]) > float(ema20.iloc[-1]))
    golden      = int(float(ema20.iloc[-1]) > float(ema50.iloc[-1]))

    # Momentum 5d / 20d
    mom5  = float((close.iloc[-1] / close.iloc[-5]  - 1) * 100) if len(close) >= 5  else 0.0
    mom20 = float((close.iloc[-1] / close.iloc[-20] - 1) * 100) if len(close) >= 20 else 0.0

    # Volume ratio vs 20d average
    vol_ratio = 1.0
    if volume is not None and len(volume) >= 20:
        avg_vol = float(volume.iloc[-20:].mean())
        if avg_vol > 0:
            vol_ratio = float(volume.iloc[-1]) / avg_vol

    # Filtros duros
    if rsi > 72:           # sobrecompra fuerte
        return None
    if rsi < 28:           # oversold extremo (caída libre)
        return None
    if mom5 < -3.0:        # baja intradía fuerte: esperar
        return None
    if mom20 < -8.0:       # tendencia mensual muy negativa
        return None

    # Score compuesto (0-1)
    # RSI zona óptima: 45-65 → 1.0; fuera decrece
    rsi_score = max(0.0, 1.0 - abs(rsi - 55) / 20)
    mom_score = min(1.0, max(0.0, (mom5 + mom20 * 0.5) / 12))
    tech_score = (macd_bull * 0.4 + above_ema20 * 0.3 + golden * 0.3)
    vol_score  = min(1.0, (vol_ratio - 0.8) / 1.2) if vol_ratio > 0.8 else 0.0

    composite = (
        rsi_score  * 0.25
        + mom_score  * 0.35
        + tech_score * 0.25
        + vol_score  * 0.15
    )

    return {
        "rsi":       round(rsi, 1),
        "macd_bull": macd_bull,
        "mom5d":     round(mom5,  2),
        "mom20d":    round(mom20, 2),
        "vol_ratio": round(vol_ratio, 2),
        "score":     round(composite, 3),
        "price":     round(float(close.iloc[-1]), 2),
    }


def main():
    parser = argparse.ArgumentParser(description="Midday CP scan.")
    parser.add_argument("--live", action="store_true", help="Ejecutar órdenes reales.")
    args = parser.parse_args()

    load_dotenv(BASE_DIR / ".env")
    log.info("=== MIDDAY SCAN START === live=%s", args.live)

    from alpha_agent.config import UNIVERSE, PARAMS
    from alpha_agent.notifications import send_notification
    from trader_agent.brokers.alpaca_broker import AlpacaBroker

    broker = AlpacaBroker(paper=True)

    # ── 1. Verificar horario ────────────────────────────────────────────────────
    try:
        if not broker.is_market_open():
            log.info("Mercado cerrado. Saliendo.")
            return
    except Exception as e:
        log.warning("No se pudo verificar horario: %s — continuando.", e)

    # ── 2. Estado de cuenta ─────────────────────────────────────────────────────
    try:
        equity    = broker.get_equity()
        positions = broker.get_positions()
    except Exception as e:
        log.error("Error conectando a Alpaca: %s", e)
        return

    current_tickers = {p.ticker for p in positions}
    log.info("Equity: $%.2f | Posiciones: %d %s", equity, len(positions), sorted(current_tickers))

    # ── 3. Calcular capital CP disponible ───────────────────────────────────────
    signals_data: dict = {}
    if SIGNALS_PATH.exists():
        try:
            signals_data = json.loads(SIGNALS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass

    capital_base = signals_data.get("capital_usd", PARAMS.paper_capital_usd)

    try:
        alloc    = json.loads((BASE_DIR / "signals" / "allocation.json").read_text(encoding="utf-8"))
        cp_pct   = float(alloc.get("cp_pct", PARAMS.weight_short_term))
        cp_max_h = int(alloc.get("cp_max_hold_days", 3))
    except Exception:
        cp_pct   = PARAMS.weight_short_term
        cp_max_h = 3

    lp_tickers   = {s["ticker"] for s in signals_data.get("long_term", [])}
    cp_positions  = [p for p in positions if p.ticker not in lp_tickers]
    n_cp_open     = len(cp_positions)
    cp_invested   = sum(abs(p.market_value) for p in cp_positions)
    cp_budget     = equity * cp_pct
    cp_available  = max(0.0, cp_budget - cp_invested)

    log.info(
        "CP: budget=$%.0f | invertido=$%.0f | disponible=$%.0f | posiciones=%d",
        cp_budget, cp_invested, cp_available, n_cp_open,
    )

    # ── 4. Macro context: no operar si mercado es desfavorable ─────────────────
    _regime = "unknown"
    _vix_now = 0.0
    try:
        from alpha_agent.macro.macro_context import fetch_macro_snapshot
        _macro = fetch_macro_snapshot()
        _vix_now = float((_macro.prices or {}).get("vix", 18) or 18)
        _regime  = (_macro.regime or "unknown").lower()

        if _regime == "bear" or _vix_now > 27:
            log.info(
                "Macro DESFAVORABLE: régimen=%s VIX=%.1f — midday scan cancelado.",
                _regime, _vix_now,
            )
            return
        log.info("Macro OK: régimen=%s VIX=%.1f — procediendo.", _regime, _vix_now)
    except Exception as _me:
        log.warning("Macro context no disponible: %s — sin filtro macro.", _me)

    # ── 5. Evaluar si hay slots y capital suficiente ────────────────────────────
    MAX_CP_SLOTS = 2
    open_slots = MAX_CP_SLOTS - n_cp_open

    # VIX moderado: conservar a 1 slot
    if _vix_now > 22:
        open_slots = min(open_slots, 1)
        log.info("VIX %.1f > 22 — limitando a 1 slot CP.", _vix_now)

    if open_slots <= 0:
        log.info("CP sleeve lleno (%d/%d slots). Sin acción.", n_cp_open, MAX_CP_SLOTS)
        return
    if cp_available < 40:
        log.info("Capital CP insuficiente ($%.0f). Sin acción.", cp_available)
        return

    # ── 5. Descarga rápida de 90d para tickers no en portfolio ─────────────────
    tickers_to_scan = [t for t in UNIVERSE if t not in current_tickers]
    if not tickers_to_scan:
        log.info("Todos los tickers ya están en el portfolio.")
        return

    log.info("Descargando 90d para %d tickers...", len(tickers_to_scan))
    import yfinance as yf
    import pandas as pd

    raw = yf.download(
        tickers_to_scan,
        period="90d",
        interval="1d",
        progress=False,
        auto_adjust=True,
        group_by="ticker",
    )

    # ── 6. Scoring técnico ──────────────────────────────────────────────────────
    candidates: list[dict] = []
    single = len(tickers_to_scan) == 1

    for ticker in tickers_to_scan:
        try:
            if single:
                df = raw
            else:
                if ticker not in raw.columns.get_level_values(0):
                    continue
                df = raw[ticker]

            df = df.dropna(subset=["Close"])
            if len(df) < 50:
                continue

            metrics = _score_ticker(
                df["Close"], df["High"], df["Low"],
                df.get("Volume"),
            )
            if metrics is None:
                continue

            candidates.append({"ticker": ticker, **metrics})
        except Exception as e:
            log.debug("Score %s: %s", ticker, e)

    if not candidates:
        log.info("Sin candidatos tras scoring. Sin acción.")
        return

    candidates.sort(key=lambda x: x["score"], reverse=True)
    log.info(
        "Top 5 candidatos: %s",
        [(c["ticker"], c["score"], f"RSI={c['rsi']}", f"mom5={c['mom5d']:+.1f}%")
         for c in candidates[:5]],
    )

    # ── Earnings filter: descartar candidatos que reportan en ≤ 2 días ────────
    try:
        from alpha_agent.analytics.earnings_calendar import has_earnings_soon
        candidates = [c for c in candidates if not has_earnings_soon(c["ticker"], days_ahead=2)]
        if not candidates:
            log.info("Sin candidatos tras filtro de earnings. Sin acción.")
            return
        log.info("Post earnings filter: %d candidatos disponibles.", len(candidates))
    except Exception as _ef:
        log.debug("Earnings filter no disponible: %s", _ef)

    # ── 7. Selección y ejecución ────────────────────────────────────────────────
    top = candidates[:open_slots]
    per_position = round(cp_available / len(top), 2)

    if per_position < 25:
        log.info("Capital por posición demasiado bajo ($%.0f). Sin acción.", per_position)
        return

    actions: list[str] = []

    for cand in top:
        ticker = cand["ticker"]
        price  = cand["price"]
        qty    = round(per_position / price, 6)

        if qty <= 0:
            continue

        icon = "📈" if cand["macd_bull"] else "📊"
        log.info(
            "%s MIDDAY CP %s | qty=%.4f @ $%.2f | score=%.3f | RSI=%.1f | mom5d=%+.1f%%",
            icon, ticker, qty, price, cand["score"], cand["rsi"], cand["mom5d"],
        )

        action_line = (
            f"{icon} *{ticker}* qty={qty:.4f} @ ${price:.2f} "
            f"| score={cand['score']:.2f} | RSI {cand['rsi']} | 5d {cand['mom5d']:+.1f}%"
        )

        if args.live:
            try:
                from alpaca.trading.requests import MarketOrderRequest
                from alpaca.trading.enums import OrderSide, TimeInForce

                order = MarketOrderRequest(
                    symbol=ticker,
                    qty=qty,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                )
                broker._trading.submit_order(order)
                actions.append("✅ COMPRA CP " + action_line)

                try:
                    from alpha_agent.analytics.trade_db import log_trade
                    log_trade(
                        ticker=ticker, side="BUY", qty=qty, price=price,
                        notional=round(qty * price, 2), sleeve="CP", status="filled",
                    )
                except Exception:
                    pass
            except Exception as e:
                log.error("Error comprando %s: %s", ticker, e)
                actions.append(f"❌ ERROR {ticker}: {e}")
        else:
            actions.append("_(dry-run)_ " + action_line)

    # ── 8. Notificación ─────────────────────────────────────────────────────────
    if actions:
        from datetime import datetime
        ts  = datetime.now().strftime("%H:%M")
        msg = (
            f"🔍 *MIDDAY SCAN* · {ts}\n"
            f"Capital CP disponible: ${cp_available:.0f} | slots: {open_slots}\n\n"
            + "\n".join(actions)
        )
        log.info("Enviando notificación...")
        send_notification(msg)
    else:
        log.info("Sin acciones ejecutadas.")

    log.info("=== MIDDAY SCAN OK ===")


if __name__ == "__main__":
    main()
