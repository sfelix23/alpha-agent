"""
Market Predictor — anticipa la dirección del mercado para los próximos 1-5 días.

Agrega señales de múltiples fuentes y usa Claude Haiku para síntesis:
  1. Sentimiento de noticias (news_fetcher + sentiment)
  2. Sentimiento Reddit (reddit_sentiment)
  3. EDGAR: actividad insider reciente
  4. Polymarket: probabilidades de eventos macro
  5. Options put/call ratio (yfinance)
  6. Fear & Greed composite (VIX + momentum + breadth)

Output: PredictionResult con direction, conviction, horizon, y reasoning.
Se integra en scoring.py como boost y en ai_report.py como sección PREDICCIÓN.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class PredictionResult:
    direction: str          # "BULLISH" | "BEARISH" | "NEUTRAL"
    conviction: float       # 0.0 – 1.0
    horizon_days: int       # 1-5
    score: float            # -1.0 (muy bajista) a +1.0 (muy alcista)
    signals: dict[str, Any] = field(default_factory=dict)
    reasoning: str = ""

    @property
    def cp_boost(self) -> float:
        """Boost para scoring CP: +0.30 si muy bullish, -0.30 si muy bearish."""
        if self.direction == "BULLISH" and self.conviction >= 0.65:
            return round(0.30 * self.conviction, 2)
        if self.direction == "BEARISH" and self.conviction >= 0.65:
            return round(-0.30 * self.conviction, 2)
        return 0.0


def _get_news_sentiment(tickers: list[str]) -> float:
    """Score promedio de sentimiento de noticias para el universo."""
    try:
        from alpha_agent.news.news_fetcher import fetch_news
        from alpha_agent.news.sentiment import score_sentiment
        scores = []
        for t in tickers[:10]:  # top 10 para no tardar
            headlines = fetch_news(t, max_headlines=5)
            if headlines:
                s = score_sentiment(headlines)
                scores.append(s.average_score)
        return round(sum(scores) / len(scores), 3) if scores else 0.0
    except Exception as e:
        log.debug("news_sentiment error: %s", e)
        return 0.0


def _get_reddit_sentiment(tickers: list[str]) -> float:
    """Score de Reddit para los tickers más activos."""
    try:
        from alpha_agent.news.reddit_sentiment import get_scores
        scores = get_scores(tickers[:15])
        vals = [v for v in scores.values() if v != 0.0]
        return round(sum(vals) / len(vals), 3) if vals else 0.0
    except Exception as e:
        log.debug("reddit_sentiment error: %s", e)
        return 0.0


def _get_edgar_signal() -> float:
    """Señal de actividad insider (compras = bullish, ventas = bearish)."""
    try:
        from alpha_agent.news.edgar_monitor import get_insider_signal
        signal = get_insider_signal()
        return float(signal) if signal is not None else 0.0
    except Exception as e:
        log.debug("edgar_signal error: %s", e)
        return 0.0


def _get_polymarket_signal() -> float:
    """Score macro desde Polymarket: probabilidad de eventos adversos (invertido)."""
    try:
        from alpha_agent.macro.polymarket import get_macro_signals
        signals = get_macro_signals()
        if not signals:
            return 0.0
        # Alta prob de recesión/vix_spike → bearish; fed_cut → bullish
        bearish = signals.get("recession", 0) + signals.get("vix_spike", 0)
        bullish = signals.get("fed_cut", 0)
        return round((bullish - bearish) / 2, 3)
    except Exception as e:
        log.debug("polymarket_signal error: %s", e)
        return 0.0


def _get_options_pcr(benchmark: str = "SPY") -> float:
    """
    Put/Call Ratio del benchmark.
    PCR > 1.2 → miedo → score positivo (contrarian: reversal alcista inminente)
    PCR < 0.7 → euforia → score negativo (contrarian: reversión bajista)
    PCR entre 0.7-1.2 → neutro
    """
    try:
        import yfinance as yf
        ticker = yf.Ticker(benchmark)
        expirations = ticker.options
        if not expirations:
            return 0.0
        # Usar la expiración más cercana con liquidez
        opt = ticker.option_chain(expirations[0])
        total_puts  = opt.puts["volume"].fillna(0).sum()
        total_calls = opt.calls["volume"].fillna(0).sum()
        if total_calls == 0:
            return 0.0
        pcr = total_puts / total_calls
        if pcr > 1.2:
            return round(min((pcr - 1.2) * 0.5, 0.4), 3)   # contrarian bullish
        if pcr < 0.7:
            return round(max((pcr - 0.7) * 0.5, -0.4), 3)  # contrarian bearish
        return 0.0
    except Exception as e:
        log.debug("options_pcr error: %s", e)
        return 0.0


def _fear_greed_score(vix: float, regime: str) -> float:
    """Score compuesto Fear & Greed basado en VIX y régimen."""
    reg = regime.upper()
    vix_score = 0.0
    if vix > 30:
        vix_score = -0.5
    elif vix > 22:
        vix_score = -0.2
    elif vix < 15:
        vix_score = 0.3
    elif vix < 18:
        vix_score = 0.1

    regime_score = {"BULL": 0.3, "NEUTRAL": 0.0, "BEAR": -0.3}.get(reg, 0.0)
    return round((vix_score + regime_score) / 2, 3)


def _ai_synthesis(signals: dict[str, Any], api_key: str) -> tuple[str, float, str]:
    """Claude Haiku sintetiza todas las señales en direction + conviction + reasoning."""
    import anthropic, json
    score = signals.get("composite_score", 0.0)
    prompt = (
        "You are a short-term market prediction AI (1-5 day horizon).\n"
        f"Signals summary:\n{json.dumps(signals, indent=2)}\n\n"
        "Composite score range: -1.0 (very bearish) to +1.0 (very bullish).\n"
        "Respond ONLY with JSON (no markdown):\n"
        '{"direction":"BULLISH|BEARISH|NEUTRAL","conviction":<0.0-1.0>,'
        '"horizon_days":<1-5>,"reasoning":"<una frase en español>"}'
    )
    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{"role": "user", "content": prompt}],
    )
    data = json.loads(resp.content[0].text.strip())
    direction  = data.get("direction", "NEUTRAL")
    conviction = max(0.0, min(1.0, float(data.get("conviction", 0.5))))
    reasoning  = data.get("reasoning", "")
    return direction, conviction, reasoning


def predict(
    tickers: list[str],
    vix: float = 20.0,
    regime: str = "NEUTRAL",
) -> PredictionResult:
    """
    Punto de entrada principal.
    Retorna PredictionResult con direction, conviction, score y reasoning.
    """
    log.info("Market Predictor: agregando señales para %d tickers...", len(tickers))

    news_score    = _get_news_sentiment(tickers)
    reddit_score  = _get_reddit_sentiment(tickers)
    edgar_score   = _get_edgar_signal()
    poly_score    = _get_polymarket_signal()
    pcr_score     = _get_options_pcr()
    fg_score      = _fear_greed_score(vix, regime)

    # Ponderación: noticias y fear/greed tienen más peso
    composite = round(
        news_score   * 0.25 +
        reddit_score * 0.15 +
        edgar_score  * 0.15 +
        poly_score   * 0.10 +
        pcr_score    * 0.20 +
        fg_score     * 0.15,
        3
    )

    signals = {
        "news_sentiment":   news_score,
        "reddit_sentiment": reddit_score,
        "insider_activity": edgar_score,
        "polymarket_macro": poly_score,
        "options_pcr":      pcr_score,
        "fear_greed":       fg_score,
        "composite_score":  composite,
        "vix":              vix,
        "regime":           regime,
    }

    log.info("Signals: %s", signals)

    direction, conviction, reasoning = _rule_fallback(composite)

    result = PredictionResult(
        direction=direction,
        conviction=conviction,
        horizon_days=3,
        score=composite,
        signals=signals,
        reasoning=reasoning,
    )
    log.info(
        "Predicción: %s (conviction=%.0f%%) | boost_CP=%+.2f | %s",
        direction, conviction * 100, result.cp_boost, reasoning,
    )
    return result


def _rule_fallback(composite: float) -> tuple[str, float, str]:
    if composite >= 0.20:
        return "BULLISH",  min(composite * 2, 0.9), f"Señales alineadas alcistas (score={composite:+.2f})."
    if composite <= -0.20:
        return "BEARISH",  min(-composite * 2, 0.9), f"Señales alineadas bajistas (score={composite:+.2f})."
    return "NEUTRAL", 0.4, f"Señales mixtas sin dirección clara (score={composite:+.2f})."
