"""
Radar de mercado — escaneo del universo completo.

A diferencia de `build_signals`, que solo analiza los tickers que pasan los
filtros de CAPM/Markowitz, este módulo recorre TODOS los activos del universo
y produce un digest liviano con:

    - movimiento de precio 1d / 1w / 1m
    - titular más reciente (si hay)
    - sentiment rápido del titular
    - acción del agente: BUY_LP / BUY_CP / CALL / PUT / HEDGE / — (ignorado)

El output se adjunta al objeto `Signals` y se renderiza en el brief de
WhatsApp como la sección "📡 RADAR".
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field

import pandas as pd

from alpha_agent.news import fetch_ticker_news, summarize_sentiment

logger = logging.getLogger(__name__)


@dataclass
class RadarEntry:
    ticker: str
    sector: str
    price: float
    pct_1d: float
    pct_1w: float
    pct_1m: float
    action: str              # "BUY_LP" | "BUY_CP" | "CALL" | "PUT" | "HEDGE" | "—"
    headline: str = ""       # titular más reciente, ya truncado
    sentiment: float = 0.0
    n_headlines: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MarketRadar:
    """Contiene la lista rankeada de entries y algunos agregados top-level."""
    entries: list[RadarEntry] = field(default_factory=list)
    n_up: int = 0
    n_down: int = 0
    biggest_winner: str = ""
    biggest_loser: str = ""

    def to_list(self) -> list[dict]:
        return [e.to_dict() for e in self.entries]


def _pct_change(series: pd.Series, periods: int) -> float:
    """Cambio porcentual usando últimos `periods` cierres (robusto a NaN)."""
    if series is None or len(series) <= periods:
        return 0.0
    try:
        last = float(series.iloc[-1])
        past = float(series.iloc[-periods - 1])
        if past == 0 or pd.isna(last) or pd.isna(past):
            return 0.0
        return (last - past) / past
    except Exception:
        return 0.0


def _action_for_ticker(ticker: str, signals) -> str:
    """Determina la acción del agente para este ticker."""
    if any(s.ticker == ticker for s in signals.long_term):
        return "BUY_LP"
    if any(s.ticker == ticker for s in signals.short_term):
        return "BUY_CP"
    for s in signals.options_book:
        if s.ticker == ticker:
            side = (s.option or {}).get("type", "").upper()
            return "CALL" if side == "CALL" else "PUT"
    if any(s.ticker == ticker for s in signals.hedge_book):
        return "HEDGE"
    return "—"


def _fetch_headline_safe(ticker: str) -> tuple[str, float, int]:
    """
    Trae el titular más reciente + sentiment + count.
    El cache de yfinance evita hits repetidos (ya se llamó en build_signals
    para los tickers elegidos, acá sólo completa los que faltaban).
    """
    try:
        hs = fetch_ticker_news(ticker)
        if not hs:
            return "", 0.0, 0
        # El más reciente
        top = hs[0]
        title = (top.title or "").strip()
        if len(title) > 75:
            title = title[:72] + "..."
        sent = summarize_sentiment(hs)
        return title, float(sent.score), int(sent.n_headlines)
    except Exception as e:
        logger.debug("Radar: news falló para %s: %s", ticker, e)
        return "", 0.0, 0


def _impact_score(entry: RadarEntry) -> float:
    """
    Score de prioridad para el ranking del radar. Prioriza:
      - movimiento absoluto 1d (lo que el usuario vio hoy)
      - movimiento absoluto 1w (tendencia de la semana)
      - tener news reciente (bonus)
      - estar en la cartera del bot (bonus alto → siempre visible)
    """
    score = abs(entry.pct_1d) * 100 + abs(entry.pct_1w) * 30
    if entry.n_headlines > 0:
        score += 5 + abs(entry.sentiment) * 10
    if entry.action != "—":
        score += 50   # posiciones abiertas siempre van arriba
    return score


def build_market_radar(
    *,
    closes: pd.DataFrame,
    signals,
    sector_map: dict[str, str],
    max_entries: int = 10,
    fetch_news: bool = True,
) -> MarketRadar:
    """
    Escanea el universo completo y arma el radar.

    Args:
        closes:      DataFrame de cierres diarios (columnas = tickers).
        signals:     Signals ya construido (para extraer la acción por ticker).
        sector_map:  Mapa ticker → sector, desde config.
        max_entries: Cuántas filas mostrar como máx en el brief.
        fetch_news:  Si False, salta el fetch (útil para tests / retries rápidos).
    """
    radar = MarketRadar()

    # Tickers en signals → siempre presentes en el radar con su action
    action_tickers = set()
    for bucket in (signals.long_term, signals.short_term, signals.options_book, signals.hedge_book):
        for s in bucket:
            action_tickers.add(s.ticker)

    for ticker in closes.columns:
        series = closes[ticker].dropna()
        if series.empty:
            continue
        price = float(series.iloc[-1])
        entry = RadarEntry(
            ticker=ticker,
            sector=sector_map.get(ticker, "Other"),
            price=round(price, 2),
            pct_1d=round(_pct_change(series, 1), 4),
            pct_1w=round(_pct_change(series, 5), 4),
            pct_1m=round(_pct_change(series, 21), 4),
            action=_action_for_ticker(ticker, signals),
        )

        # Fetch news sólo para tickers con movimiento significativo o ya en cartera
        needs_news = (
            fetch_news and (
                abs(entry.pct_1d) >= 0.02   # ±2% diario
                or abs(entry.pct_1w) >= 0.05   # ±5% semanal
                or ticker in action_tickers
            )
        )
        if needs_news:
            headline, sent, n = _fetch_headline_safe(ticker)
            entry.headline = headline
            entry.sentiment = sent
            entry.n_headlines = n

        radar.entries.append(entry)

    # Agregados top-level
    ups = [e for e in radar.entries if e.pct_1d > 0]
    downs = [e for e in radar.entries if e.pct_1d < 0]
    radar.n_up = len(ups)
    radar.n_down = len(downs)
    if ups:
        winner = max(ups, key=lambda e: e.pct_1d)
        radar.biggest_winner = f"{winner.ticker} {winner.pct_1d*100:+.1f}%"
    if downs:
        loser = min(downs, key=lambda e: e.pct_1d)
        radar.biggest_loser = f"{loser.ticker} {loser.pct_1d*100:+.1f}%"

    # Rankeo por impacto y recorte
    radar.entries.sort(key=_impact_score, reverse=True)
    radar.entries = radar.entries[:max_entries]

    logger.info(
        "Radar: %d activos escaneados, %d ↑ / %d ↓, top movers: %s / %s",
        len(closes.columns), radar.n_up, radar.n_down,
        radar.biggest_winner, radar.biggest_loser,
    )
    return radar
