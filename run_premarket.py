"""
Pre-Market Gap Scanner — corre a las 9:00 AM ART (8:00 AM EDT).

Detecta tickers del universo DT con gap pre-market > 2% antes de la apertura.
Manda WhatsApp + Telegram con la lista para revisión manual antes de que el
sistema entre automáticamente a las 11:30.

No ejecuta órdenes — solo informa.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.platform == "win32" and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s — %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("premarket")

# Universo combinado: DT + LP/CP del universo principal más líquidos
SCAN_UNIVERSE = [
    "NVDA", "AMD", "TSLA", "AAPL", "AMZN", "META", "GOOGL", "MSFT",
    "COIN", "PLTR", "AVGO", "TSM",
    "XOM", "CVX", "SLB", "PBR", "VIST",
    "GD", "LMT", "RTX", "NOC",
    "MELI", "DESP", "GGAL",
    "GOLD", "NEM", "FCX", "SQM",
    "SPY", "QQQ",
]

MIN_GAP_PCT = 0.020   # 2% gap mínimo para alertar
MAX_PRICE   = 500.0   # por encima de esto las shares son pocas para $1400


def _fetch_premarket_gap(ticker: str) -> float | None:
    """
    Descarga datos pre-market vía yfinance (period=2d, interval=1m).
    Gap = (pre_market_last - prev_close) / prev_close.
    Devuelve None si no hay datos suficientes.
    """
    try:
        import warnings
        import pandas as pd
        import yfinance as yf
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            df = yf.download(ticker, period="2d", interval="1m",
                             progress=False, auto_adjust=True, prepost=True)
        if df is None or df.empty or len(df) < 10:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        today_str = df.index[-1].strftime("%Y-%m-%d")
        today_df  = df[df.index.strftime("%Y-%m-%d") == today_str]
        prev_df   = df[df.index.strftime("%Y-%m-%d") < today_str]

        if today_df.empty or prev_df.empty:
            return None

        prev_close = float(prev_df["Close"].iloc[-1])
        premarket_last = float(today_df["Close"].iloc[-1])
        if prev_close <= 0:
            return None
        return (premarket_last - prev_close) / prev_close
    except Exception as exc:
        log.debug("%s gap error: %s", ticker, exc)
        return None


def main() -> None:
    log.info("PRE-MARKET SCANNER — %s", datetime.now().strftime("%Y-%m-%d %H:%M"))

    gaps: list[tuple[float, str, str]] = []  # (gap_pct, ticker, direction)

    for ticker in SCAN_UNIVERSE:
        gap = _fetch_premarket_gap(ticker)
        if gap is None:
            continue
        if abs(gap) >= MIN_GAP_PCT:
            direction = "GAP UP" if gap > 0 else "GAP DOWN"
            gaps.append((gap, ticker, direction))
            log.info("  %s %s: %+.1f%%", direction, ticker, gap * 100)

    gaps.sort(key=lambda x: abs(x[0]), reverse=True)

    if not gaps:
        log.info("Sin gaps significativos (>2%%) pre-market hoy.")
        return

    # Construir mensaje
    ts = datetime.now().strftime("%H:%M")
    lines = [f"PRE-MARKET | {ts} ART | {len(gaps)} alertas"]
    for gap_pct, ticker, direction in gaps[:8]:
        icon = "🔥" if gap_pct > 0 else "💥"
        lines.append(f"  {icon} {ticker}: {gap_pct*100:+.1f}% {direction}")

    lines.append("")
    lines.append("DayTrader entra 11:30 ART. Revisá antes si algo es raro.")

    msg = "\n".join(lines)
    log.info("Enviando alerta pre-market (%d gaps)...", len(gaps))

    from alpha_agent.notifications import send_notification
    send_notification(msg)
    log.info("Pre-market alert enviada.")


if __name__ == "__main__":
    main()
