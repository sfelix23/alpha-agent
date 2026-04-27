"""
Señales estructuradas — el contrato entre el alpha_agent y el trader_agent.

Cada señal lleva ahora una `TradeThesis` completa con bloques quant / technical /
fundamental / macro / risk, más una narrativa en español. Así el Agente 2 no
solo sabe qué operar sino POR QUÉ.

Schema (JSONable):
{
  "generated_at", "horizon", "capital_usd", "params",
  "macro": { regime, prices, changes_1m, ... },
  "long_term":  [ Signal, ... ],
  "short_term": [ Signal, ... ],
  "portfolio":  { ticker: weight, ... }   # Markowitz LP (debug)
}
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from alpha_agent.config import PARAMS, PATHS
from alpha_agent.macro.macro_context import MacroSnapshot
from alpha_agent.news import fetch_ticker_news, summarize_sentiment
from alpha_agent.reasoning import TradeThesis, build_trade_thesis

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    """
    Señal única. Cubre tanto equity como opciones.

    side:
        "BUY"        → comprar acción cash (long equity)
        "SELL_SHORT" → vender en corto acción (solo si PARAMS.enable_short_equity)
        "BUY_CALL"   → comprar opción call (direccional alcista apalancado)
        "BUY_PUT"    → comprar opción put (direccional bajista o hedge)
        "HOLD"/"SELL" → marcas auxiliares

    horizon:
        "LP"    → largo plazo equity
        "CP"    → corto plazo equity técnico
        "DERIV" → direccional con opciones (bullish o bearish)
        "HEDGE" → puts de cobertura de cartera (ej: puts SPY)
    """
    ticker: str
    side: str
    horizon: str
    price: float
    stop_loss: float | None
    take_profit: float | None
    weight_target: float     # 0..1, fracción del sleeve
    thesis: dict = field(default_factory=dict)   # TradeThesis.to_dict()
    # Bloque opcional para opciones. None si es equity.
    option: dict | None = None


@dataclass
class Signals:
    generated_at: str
    horizon: str
    capital_usd: float
    params: dict
    macro: dict = field(default_factory=dict)
    long_term: list[Signal] = field(default_factory=list)
    short_term: list[Signal] = field(default_factory=list)
    options_book: list[Signal] = field(default_factory=list)   # calls/puts direccionales
    hedge_book: list[Signal] = field(default_factory=list)     # puts SPY de cobertura
    portfolio: dict[str, float] = field(default_factory=dict)
    radar: dict = field(default_factory=dict)
    edgar_alerts: list[dict] = field(default_factory=list)     # 8-K materiales detectados hoy

    def to_json(self) -> str:
        payload = {
            "generated_at": self.generated_at,
            "horizon": self.horizon,
            "capital_usd": self.capital_usd,
            "params": self.params,
            "macro": self.macro,
            "long_term": [asdict(s) for s in self.long_term],
            "short_term": [asdict(s) for s in self.short_term],
            "options_book": [asdict(s) for s in self.options_book],
            "hedge_book": [asdict(s) for s in self.hedge_book],
            "portfolio": self.portfolio,
            "radar": self.radar,
            "edgar_alerts": self.edgar_alerts,
        }
        return json.dumps(payload, indent=2, ensure_ascii=False, default=_json_default)

    def save(self, path: Path | None = None) -> Path:
        if path is None:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = PATHS.signals_dir / f"signals_{ts}.json"
        path.write_text(self.to_json(), encoding="utf-8")
        (PATHS.signals_dir / "latest.json").write_text(self.to_json(), encoding="utf-8")
        return path


def _json_default(o):
    if isinstance(o, (np.floating, np.integer)):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (pd.Timestamp, datetime)):
        return o.isoformat()
    raise TypeError(f"Tipo no serializable: {type(o)}")


# ────────────────────────────────────────────────────────────────────────────
# Construcción de señales (enriquecidas con news, macro, reasoning)
# ────────────────────────────────────────────────────────────────────────────

def _make_signal(
    *,
    ticker: str,
    horizon: str,
    quant_row: pd.Series,
    tech_row: pd.Series,
    weight: float,
    macro: MacroSnapshot,
    capital: float,
) -> Signal:
    # Fetch news sentiment del ticker
    try:
        headlines = fetch_ticker_news(ticker)
    except Exception as e:
        logger.debug("News fetch falló para %s: %s", ticker, e)
        headlines = []
    sentiment = summarize_sentiment(headlines)

    thesis: TradeThesis = build_trade_thesis(
        ticker=ticker,
        horizon=horizon,
        quant_row=quant_row,
        tech_row=tech_row,
        sentiment=sentiment,
        macro=macro,
        weight_target=weight,
        capital=capital,
    )

    price = float(tech_row.get("price", 0) or 0)
    stop = tech_row.get("stop_loss_atr")
    stop_val = float(stop) if stop is not None and not (isinstance(stop, float) and np.isnan(stop)) else None

    if horizon == "LP":
        # LP: TP dinámico por beta — alta convicción merece más upside
        beta = float(quant_row.get("beta", 1.0) or 1.0)
        tp_pct = 0.22 if beta >= 1.5 else (0.18 if beta >= 1.0 else 0.14)
        tp = round(price * (1 + tp_pct), 2) if price else None
    else:
        # CP: stop ATR-adaptivo con rango [-3%, -8%]
        # Piso -3%: no más apretado (evita whipsaw en stocks volátiles)
        # Techo -8%: no más ancho (limita pérdida por trade)
        # TP = 2.5×riesgo asumido, mínimo +8%
        if price:
            floor_stop = price * 0.92  # stop nunca más ancho que -8%
            ceil_stop  = price * 0.97  # stop nunca más apretado que -3%
            if stop_val:
                stop_val = round(max(floor_stop, min(stop_val, ceil_stop)), 2)
            else:
                stop_val = round(price * 0.94, 2)  # default -6% si no hay ATR

        risk = (price - stop_val) if (stop_val and price) else price * 0.06
        atr_tp = price + 2.5 * risk                  # 2.5:1 R/R mínimo
        # RSI extremo → TP acelerado: capturar ganancia antes de mean-reversion
        # RSI > 80 en bull fuerte puede seguir, pero estadísticamente revierte más rápido
        rsi_val = float(tech_row.get("rsi", 50) or 50)
        min_tp = price * (1.05 if rsi_val > 80 else 1.08)
        tp = round(max(atr_tp, min_tp), 2) if price else None

    return Signal(
        ticker=ticker,
        side="BUY",
        horizon=horizon,
        price=round(price, 2),
        stop_loss=stop_val,
        take_profit=tp,
        weight_target=round(float(weight), 4),
        thesis=thesis.to_dict(),
    )


def build_signals(
    scores: dict[str, pd.DataFrame],
    portfolio_lp: dict,
    *,
    macro: MacroSnapshot,
    capital: float | None = None,
) -> Signals:
    """
    Combina rankings + cartera optimizada + noticias + macro en un Signals listo.
    """
    cap = capital if capital is not None else PARAMS.paper_capital_usd

    sig = Signals(
        generated_at=datetime.now().isoformat(timespec="seconds"),
        horizon="medium_long_70_30_high_conviction",
        capital_usd=cap,
        params={
            "weight_long_term": PARAMS.weight_long_term,
            "weight_short_term": PARAMS.weight_short_term,
            "weight_options": PARAMS.weight_options,
            "risk_free_rate": PARAMS.risk_free_rate,
            "max_beta_lp": PARAMS.max_beta_lp,
            "min_sharpe_lp": PARAMS.min_sharpe_lp,
            "top_n_long_term": PARAMS.top_n_long_term,
            "top_n_short_term": PARAMS.top_n_short_term,
            "top_n_bearish": PARAMS.top_n_bearish,
            "max_weight_per_asset": PARAMS.max_weight_per_asset,
            "enable_short_equity": PARAMS.enable_short_equity,
            "enable_options": PARAMS.enable_options,
        },
        macro={
            "regime": macro.regime,
            "regime_reason": macro.regime_reason,
            "prices": macro.prices,
            "changes_1d": macro.changes_1d,
            "changes_1m": macro.changes_1m,
            "spy_vs_sma200": macro.spy_vs_sma200,
        },
    )

    # ── LP: top N por peso Markowitz, excluyendo earnings inminentes ────
    lp_df = scores["long_term"]
    weights_series = portfolio_lp["weights"]

    # Pre-filtrar tickers bloqueados por earnings antes de tomar top N
    # así el 2do slot usa el siguiente candidato elegible en vez de quedar vacío
    earnings_blocked = set()
    if "earnings_soon" in lp_df.columns:
        earnings_blocked = set(lp_df[lp_df["earnings_soon"] == 1].index)
    if earnings_blocked:
        import logging as _lg
        _lg.getLogger(__name__).warning("LP earnings filter: %s bloqueados", sorted(earnings_blocked))

    eligible_lp = weights_series[
        (weights_series > 0) & (~weights_series.index.isin(earnings_blocked))
    ].head(PARAMS.top_n_long_term)
    if eligible_lp.sum() > 0:
        eligible_lp = eligible_lp / eligible_lp.sum()

    for ticker, w in eligible_lp.items():
        if ticker not in lp_df.index:
            continue
        tech_row = scores["short_term"].loc[ticker] if ticker in scores["short_term"].index else pd.Series(dtype=float)
        sig.long_term.append(_make_signal(
            ticker=ticker, horizon="LP",
            quant_row=lp_df.loc[ticker],
            tech_row=tech_row,
            weight=float(w),
            macro=macro,
            capital=cap,
        ))

    # Ajuste por convicción LP
    sig.long_term = _apply_conviction_weights(sig.long_term)

    # cartera completa (debug)
    sig.portfolio = {t: round(float(w), 4) for t, w in weights_series.items() if w > 0.01}

    # ── CP: top N por score_st con pesos proporcionales ───────────────
    # Excluir tickers ya elegidos como LP para evitar doble sizing del mismo activo.
    lp_tickers = {s.ticker for s in sig.long_term}
    st_df = scores["short_term"]
    st_df_eligible = st_df[~st_df.index.isin(lp_tickers)]
    top_st = st_df_eligible.head(PARAMS.top_n_short_term)
    raw = top_st["score_st"].clip(lower=0)
    if raw.sum() > 0:
        weights_st = raw / raw.sum()
    else:
        weights_st = pd.Series(1.0 / max(len(top_st), 1), index=top_st.index)

    for ticker, row in top_st.iterrows():
        sig.short_term.append(_make_signal(
            ticker=ticker, horizon="CP",
            quant_row=row,
            tech_row=row,
            weight=float(weights_st.get(ticker, 1.0 / max(len(top_st), 1))),
            macro=macro,
            capital=cap,
        ))

    # Ajuste por convicción CP
    sig.short_term = _apply_conviction_weights(sig.short_term)

    return sig


def _apply_conviction_weights(signals: list[Signal]) -> list[Signal]:
    """
    Repondera los weight_target según la convicción de cada señal.
    ALTA → ×1.5 | MEDIA → ×1.0 | BAJA → ×0.6
    Los pesos se renormalizan para que sumen 1.0.
    """
    _MULT = {"ALTA": 1.5, "MEDIA": 1.0, "BAJA": 0.6}
    if not signals:
        return signals

    adjusted: list[float] = []
    for s in signals:
        conv = s.thesis.get("conviction", "MEDIA")
        adjusted.append(s.weight_target * _MULT.get(conv, 1.0))

    total = sum(adjusted) or 1.0
    for s, w in zip(signals, adjusted):
        s.weight_target = round(w / total, 4)

    logger.info(
        "Conviction weights: %s",
        {s.ticker: f"{s.weight_target:.2%} ({s.thesis.get('conviction','?')})" for s in signals},
    )
    return signals
