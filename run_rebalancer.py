"""
Agente 4 — Rebalancer semanal de cartera.

Corre los viernes a las 15:00 ART (vía Task Scheduler).
Compara pesos actuales en Alpaca contra targets del último
signals/latest.json. Si un activo se desvió más del umbral, ajusta.

Uso:
    python run_rebalancer.py              # dry-run
    python run_rebalancer.py --live       # ejecuta en Alpaca paper
    python run_rebalancer.py --threshold 0.05
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR     = Path(__file__).parent.resolve()
SIGNALS_PATH = BASE_DIR / "signals" / "latest.json"
LOG_DIR      = BASE_DIR / "logs"

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.platform == "win32" and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logger = logging.getLogger("rebalancer")


def setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / f"rebalancer_{datetime.now().strftime('%Y-%m-%d')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(str(log_file), encoding="utf-8"),
        ],
    )


def load_targets(path: Path) -> dict[str, dict]:
    """Devuelve {ticker: portfolio_weight} desde latest.json."""
    if not path.exists():
        return {}
    try:
        data   = json.loads(path.read_text(encoding="utf-8"))
        params = data.get("params", {})
    except Exception as e:
        logger.warning("No se pudo leer signals: %s", e)
        return {}

    sleeve_map = {
        "long_term":  params.get("weight_long_term",  0.55),
        "short_term": params.get("weight_short_term", 0.25),
    }
    targets: dict[str, dict] = {}
    for bucket, sleeve_w in sleeve_map.items():
        for sig in data.get(bucket, []):
            ticker = sig.get("ticker")
            wt     = sig.get("weight_target", 0)
            if ticker and wt > 0:
                targets[ticker] = {
                    "portfolio_weight": wt * sleeve_w,
                    "horizon": bucket,
                }
    return targets


def _spy_weekly_return() -> float | None:
    """Retorna el retorno semanal de SPY (5 días)."""
    try:
        import yfinance as yf
        spy = yf.download("SPY", period="5d", progress=False, auto_adjust=True)
        if spy is not None and len(spy) >= 2:
            first = float(spy["Close"].iloc[0])
            last  = float(spy["Close"].iloc[-1])
            return (last - first) / first * 100 if first > 0 else None
    except Exception as e:
        logger.warning("SPY weekly return: %s", e)
    return None


def _portfolio_weekly_return(broker) -> float | None:
    """Retorna el retorno semanal del portfolio desde el historial de Alpaca."""
    try:
        from alpaca.trading.requests import GetPortfolioHistoryRequest
        req = GetPortfolioHistoryRequest(period="1W", timeframe="1D")
        ph  = broker._trading.get_portfolio_history(req)
        if ph and ph.equity:
            vals = [float(e) for e in ph.equity if e is not None and float(e) > 0]
            if len(vals) >= 2:
                return (vals[-1] - vals[0]) / vals[0] * 100 if vals[0] > 0 else None
    except Exception as e:
        logger.warning("Portfolio weekly history: %s", e)
    return None


def _save_performance_log(port_ret: float | None, spy_ret: float | None) -> None:
    """Acumula el tracking semanal vs SPY en signals/performance_log.json."""
    log_path = BASE_DIR / "signals" / "performance_log.json"
    try:
        existing = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else {"weeks": []}
    except Exception:
        existing = {"weeks": []}
    existing["weeks"].append({
        "date": datetime.now().strftime("%Y-%m-%d"),
        "portfolio_pct": round(port_ret, 2) if port_ret is not None else None,
        "spy_pct": round(spy_ret, 2) if spy_ret is not None else None,
        "alpha_pct": round(port_ret - spy_ret, 2) if (port_ret and spy_ret) else None,
    })
    # Keep last 52 weeks
    existing["weeks"] = existing["weeks"][-52:]
    log_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Performance log actualizado: %d semanas", len(existing["weeks"]))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live",        action="store_true")
    parser.add_argument("--threshold",   type=float, default=0.08)
    parser.add_argument("--no-discovery", action="store_true", help="Saltar el discovery scan")
    args = parser.parse_args()

    load_dotenv(BASE_DIR / ".env")
    setup_logging()
    logger.info("=== REBALANCER START === live=%s threshold=%.1f%%",
                args.live, args.threshold * 100)

    from trader_agent.brokers.alpaca_broker import AlpacaBroker
    from alpha_agent.notifications import send_whatsapp

    broker = AlpacaBroker(paper=True)

    try:
        equity    = broker.get_equity()
        positions = broker.get_positions()
    except Exception as e:
        logger.error("Error conectando a Alpaca: %s", e)
        return

    if not positions:
        logger.info("Sin posiciones abiertas.")
        return

    targets  = load_targets(SIGNALS_PATH)
    if not targets:
        logger.warning("Sin targets en signals/latest.json")
        return

    sells:        list[dict] = []
    buys:         list[dict] = []
    report_lines: list[str]  = []

    equity_positions = {p.ticker: p for p in positions
                        if getattr(p, "asset_class", "equity") == "equity"}

    for ticker, pos in equity_positions.items():
        current_w   = pos.market_value / equity if equity > 0 else 0
        target_info = targets.get(ticker)
        if not target_info:
            report_lines.append(f"  ⚪ {ticker}: sin señal activa")
            continue

        target_w = target_info["portfolio_weight"]
        drift    = current_w - target_w

        if abs(drift) < args.threshold:
            report_lines.append(
                f"  ✅ {ticker}: {current_w:.1%} ≈ {target_w:.1%} (Δ{drift:+.1%})"
            )
            continue

        notional = abs(drift) * equity
        entry    = {"ticker": ticker, "notional": notional,
                    "drift": drift, "current_w": current_w, "target_w": target_w}
        if drift > 0:
            sells.append(entry)
            report_lines.append(
                f"  🔴 SELL {ticker}: ${notional:.0f} ({current_w:.1%}→{target_w:.1%})"
            )
        else:
            buys.append(entry)
            report_lines.append(
                f"  🟢 BUY  {ticker}: ${notional:.0f} ({current_w:.1%}→{target_w:.1%})"
            )

    executed: list[str] = []
    errors:   list[str] = []

    if args.live and (sells or buys):
        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        for entry in sells + buys:           # SELLs primero → libera cash
            ticker   = entry["ticker"]
            side     = OrderSide.SELL if entry["drift"] > 0 else OrderSide.BUY
            try:
                price = broker.get_last_price(ticker)
                qty   = round(entry["notional"] / price, 4) if price > 0 else 0
                if qty <= 0:
                    continue
                # Limit order para reducir slippage vs market
                lp = round(price * (1.0015 if side == OrderSide.BUY else 0.9985), 2)
                broker._trading.submit_order(LimitOrderRequest(
                    symbol=ticker, qty=qty, side=side,
                    time_in_force=TimeInForce.DAY,
                    limit_price=lp,
                ))
                executed.append(f"{side.value} {qty:.4f} {ticker} @${lp:.2f}")
            except Exception as e:
                errors.append(f"{ticker}: {e}")
                logger.error("Error %s: %s", ticker, e)

    # ── Benchmark semanal vs SPY ──
    port_ret_w = _portfolio_weekly_return(broker)
    spy_ret_w  = _spy_weekly_return()
    _save_performance_log(port_ret_w, spy_ret_w)

    perf_line = ""
    if port_ret_w is not None and spy_ret_w is not None:
        alpha_w = port_ret_w - spy_ret_w
        sign = "+" if alpha_w >= 0 else ""
        emoji = "🟢" if alpha_w >= 0 else "🔴"
        perf_line = (
            f"\n📊 *SEMANA*: Portfolio {port_ret_w:+.1f}% vs SPY {spy_ret_w:+.1f}%"
            f" → Alpha {sign}{alpha_w:.1f}% {emoji}"
        )
        logger.info("Semana: portfolio %+.1f%%, SPY %+.1f%%, alpha %+.1f%%",
                    port_ret_w, spy_ret_w, alpha_w)

    # ── Discovery scan de nuevas oportunidades ──
    discovery_lines = ""
    if not args.no_discovery:
        try:
            signals_data = {}
            if SIGNALS_PATH.exists():
                signals_data = json.loads(SIGNALS_PATH.read_text(encoding="utf-8"))
            macro_ctx = signals_data.get("macro", {})
            macro_for_scan = {
                "regime": macro_ctx.get("regime", "unknown"),
                "vix": (macro_ctx.get("prices") or {}).get("vix", 20),
            }
            from alpha_agent.discovery.universe_scanner import (
                scan_for_candidates, format_whatsapp_discovery
            )
            disc_result = scan_for_candidates(macro_context=macro_for_scan)
            discovery_lines = format_whatsapp_discovery(disc_result)
            logger.info("Discovery: %d picks", len(disc_result.get("candidates", [])))
        except Exception as e:
            logger.warning("Discovery scan error: %s", e)

    n_adj = len(sells) + len(buys)
    if n_adj > 0 or errors or perf_line or discovery_lines:
        now = datetime.now().strftime("%d-%b %H:%M").lower()
        msg = "\n".join(filter(None, [
            f"⚖️ *REBALANCER* · {now}",
            f"${equity:.0f} · umbral {args.threshold:.0%}",
            perf_line,
            "",
            *report_lines,
            *(["", f"✅ {', '.join(executed)}"] if executed else []),
            *(["", f"❌ {', '.join(errors)}"]   if errors   else []),
            *(["\n_(dry-run)_"] if not args.live else []),
            discovery_lines,
        ]))
        try:
            send_whatsapp(msg)
        except Exception as e:
            logger.error("WhatsApp error: %s", e)
    else:
        logger.info("Cartera dentro de tolerancia. Sin rebalanceo.")

    logger.info("=== REBALANCER OK === ajustes=%d ejecutados=%d", n_adj, len(executed))


if __name__ == "__main__":
    main()
