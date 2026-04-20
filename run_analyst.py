"""
Entry point del Agente 1 (analista).

Pipeline:
    1. Descargar universo (con cache pickle).
    2. Descargar snapshot macro (commodities, VIX, DXY, yields, régimen de mercado).
    3. CAPM por activo.
    4. Indicadores técnicos.
    5. Scoring LP / CP.
    6. Optimización Markowitz sobre candidatos LP.
    7. Fetch de noticias por activo seleccionado + sentiment.
    8. Construcción de señales con TradeThesis embebida (quant + technical +
       news + macro + risk management).
    9. Guardar signals/latest.json.
   10. Reporte ejecutivo (Gemini con fallback determinístico).
   11. Opcional: WhatsApp.

Uso:
    python run_analyst.py                 # pide confirmación WhatsApp
    python run_analyst.py --send          # manda WhatsApp sin preguntar
    python run_analyst.py --no-ai         # no llama a Gemini
    python run_analyst.py --capital 1000  # override del capital paper
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime


# Fix para Windows cp1252: forzar stdout/stderr a UTF-8
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.platform == "win32" and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
from dotenv import load_dotenv

from alpha_agent.analytics import (
    build_scores,
    blend_markowitz_kelly,
    compute_capm_metrics,
    compute_technical_indicators,
    optimize_portfolio,
)
from alpha_agent.config import PARAMS
from alpha_agent.data import download_universe, load_benchmark
from alpha_agent.derivatives import (
    build_bearish_candidates,
    build_directional_options_signals,
    build_hedge_signals,
)
from alpha_agent.macro import fetch_macro_snapshot
from alpha_agent.config import SECTOR_MAP
from alpha_agent.notifications import send_whatsapp
from alpha_agent.radar import build_market_radar
from alpha_agent.reporting import build_signals, generate_executive_report
from alpha_agent.reporting.ai_report import signals_to_compact_brief, signals_to_whatsapp_brief


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Agente Alpha — análisis cuantitativo del universo.")
    parser.add_argument("--send", action="store_true", help="Enviar a WhatsApp sin preguntar.")
    parser.add_argument("--no-send", action="store_true", help="No enviar a WhatsApp ni preguntar.")
    parser.add_argument("--no-ai", action="store_true", help="No usar Gemini, brief determinístico.")
    parser.add_argument("--capital", type=float, default=None, help="Override del capital paper en USD.")
    args = parser.parse_args()

    load_dotenv()
    setup_logging()
    log = logging.getLogger("alpha_agent")

    capital = args.capital if args.capital else PARAMS.paper_capital_usd
    log.info("🏛️  INICIANDO AGENTE ALPHA — %s", datetime.now().isoformat(timespec="seconds"))
    log.info("💰 Capital paper: $%.2f USD", capital)

    # 1. Datos
    closes, ohlc = download_universe()
    benchmark = load_benchmark(closes)
    log.info("Universo cargado: %d activos con datos válidos", closes.shape[1])

    # 2. Macro snapshot
    log.info("🌎 Descargando contexto macro…")
    macro = fetch_macro_snapshot()
    log.info("Régimen de mercado: %s — %s", macro.regime, macro.regime_reason)

    # 3. CAPM
    capm = compute_capm_metrics(closes, benchmark)
    log.info("Métricas CAPM calculadas para %d activos", len(capm))

    # 4. Técnicos
    technical = compute_technical_indicators(ohlc)
    log.info("Indicadores técnicos calculados para %d activos", len(technical))

    # 5. Scoring (con guard de correlación y sector)
    scores = build_scores(capm, technical, closes=closes)
    log.info("Top LP post-guard: %s", scores["long_term"].head(PARAMS.top_n_long_term).index.tolist())
    log.info("Top CP: %s", scores["short_term"].head(PARAMS.top_n_short_term).index.tolist())

    # 6. Markowitz
    lp_candidates = scores["long_term"].index.tolist()
    if len(lp_candidates) >= 2:
        portfolio_lp = optimize_portfolio(closes, capm["mu_anual"], candidates=lp_candidates)
        log.info(
            "Cartera Markowitz: ret=%.2f%% vol=%.2f%% sharpe=%.2f",
            portfolio_lp["exp_return"] * 100,
            portfolio_lp["volatility"] * 100,
            portfolio_lp["sharpe"],
        )
    else:
        log.warning("Pocos candidatos LP — saltando Markowitz.")
        portfolio_lp = {"weights": pd.Series(dtype=float), "exp_return": 0, "volatility": 0, "sharpe": 0}

    # 6.5 Kelly blend — combina pesos Markowitz con Kelly Criterion (half-Kelly)
    if portfolio_lp["weights"].sum() > 0:
        portfolio_lp["weights"] = blend_markowitz_kelly(portfolio_lp["weights"], capm)
        log.info(
            "Pesos Kelly-Markowitz blended: %s",
            {t: f"{w:.2%}" for t, w in portfolio_lp["weights"][portfolio_lp["weights"] > 0].items()},
        )

    # 7-8. Señales enriquecidas con noticias + macro + reasoning
    log.info("📰 Fetching noticias y construyendo tesis por activo…")
    signals = build_signals(scores, portfolio_lp, macro=macro, capital=capital)

    # 8.5 Derivatives: bearish scoring + hedge layer + directional options
    if PARAMS.enable_options:
        log.info("🎯 Bucket opciones: evaluando candidatos bearish y hedge…")
        # Para el bearish scoring necesitamos sentiments por ticker;
        # extraemos los que ya calculó build_signals de las tesis LP/CP.
        sentiments_lookup: dict[str, float] = {}
        for s in signals.long_term + signals.short_term:
            sc = s.thesis.get("fundamental", {}).get("sentiment_score")
            if sc is not None:
                sentiments_lookup[s.ticker] = float(sc)

        bearish_df = build_bearish_candidates(
            capm=capm,
            technical=technical,
            sentiments=sentiments_lookup,
            macro=macro,
        )

        # Bullish para calls: top candidates del pool LP que NO están en el
        # top equity (para no duplicar la misma apuesta larga).
        equity_lp_tickers = {s.ticker for s in signals.long_term}
        bullish_for_calls = scores["long_term"][
            ~scores["long_term"].index.isin(equity_lp_tickers)
        ].copy()
        # agregar columnas technical que options_builder espera
        for col in ("price", "rsi", "ret_1m", "sigma_anual"):
            if col not in bullish_for_calls.columns and col in scores["short_term"].columns:
                bullish_for_calls[col] = scores["short_term"][col]

        opt_signals = build_directional_options_signals(
            bullish_candidates=bullish_for_calls,
            bearish_candidates=bearish_df,
            macro=macro,
            capital=capital,
        )
        signals.options_book = opt_signals
        log.info("Options book: %d señales direccionales", len(opt_signals))

        # Hedge layer sobre SPY
        if "SPY" in closes.columns:
            spy_spot = float(closes["SPY"].iloc[-1])
            spy_returns = closes["SPY"].pct_change().dropna()
            spy_sigma = float(spy_returns.std() * (PARAMS.trading_days ** 0.5))
            hedge_signals = build_hedge_signals(
                spy_spot=spy_spot,
                spy_sigma=spy_sigma,
                macro=macro,
                capital=capital,
            )
            signals.hedge_book = hedge_signals
            log.info("Hedge book: %d señales", len(hedge_signals))

    # 8.9 Radar de mercado: escaneo noticioso + movers del universo completo
    log.info("📡 Construyendo radar del universo (%d activos)…", closes.shape[1])
    radar = build_market_radar(
        closes=closes, signals=signals, sector_map=SECTOR_MAP, max_entries=10,
    )
    signals.radar = {
        "entries": radar.to_list(),
        "n_up": radar.n_up,
        "n_down": radar.n_down,
        "biggest_winner": radar.biggest_winner,
        "biggest_loser": radar.biggest_loser,
    }
    log.info("Radar: %d↑ / %d↓ · Winner: %s · Loser: %s",
             radar.n_up, radar.n_down, radar.biggest_winner, radar.biggest_loser)

    # 9. Guardar
    path = signals.save()
    log.info("Señales guardadas en %s", path)

    # 10. Reporte — terminal muestra la versión detallada, WhatsApp la simple
    reporte_detallado = signals_to_compact_brief(signals)
    reporte_whatsapp = signals_to_whatsapp_brief(signals)

    print("\n" + "=" * 70)
    print(reporte_detallado)
    print("=" * 70 + "\n")
    print("PREVIEW del mensaje de WhatsApp:")
    print("-" * 70)
    print(reporte_whatsapp)
    print("-" * 70 + "\n")

    # 11. WhatsApp — siempre manda la versión simplificada
    if args.send:
        send_whatsapp(reporte_whatsapp)
    elif not args.no_send:
        try:
            envio = input("¿Enviar este reporte a WhatsApp? (s/n): ")
            if envio.strip().lower().startswith("s"):
                send_whatsapp(reporte_whatsapp)
        except EOFError:
            pass


if __name__ == "__main__":
    main()
