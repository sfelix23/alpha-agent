"""
Tesis financiera por activo — el "por qué" detrás de cada señal.

Combina:
    - Quant (CAPM/Markowitz): Sharpe, beta, alfa de Jensen, retorno esperado.
    - Technical: RSI, ATR, momentum 1m/3m, distancia al máximo 52w.
    - News sentiment del ticker.
    - Contexto macro (régimen, sectores relacionados).
    - Risk management (stop, riesgo por trade, position size sobre capital).

Devuelve un objeto `TradeThesis` que se serializa dentro de cada Signal.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import pandas as pd

from alpha_agent.config import PARAMS
from alpha_agent.macro.macro_context import MacroSnapshot
from alpha_agent.news.sentiment import SentimentSummary


@dataclass
class TradeThesis:
    ticker: str
    horizon: str                           # "LP" | "CP"
    conviction: str                        # "ALTA" | "MEDIA" | "BAJA"

    # Bloques de razonamiento
    quant: dict = field(default_factory=dict)
    technical: dict = field(default_factory=dict)
    fundamental: dict = field(default_factory=dict)
    macro: dict = field(default_factory=dict)
    risk: dict = field(default_factory=dict)

    # Narrativa final en español
    thesis_text: str = ""
    key_risks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _conviction_from_scores(sharpe: float, sentiment: float, regime: str, horizon: str) -> str:
    """
    Convicción = composite simple. Más info → más peso a esa dimensión.
    - Sharpe alto (>1) suma. Sharpe bajo (<0.3) resta.
    - Sentiment positivo suma; negativo resta.
    - Regime bull favorece LP; bear penaliza LP; sideways neutral.
    """
    score = 0
    if sharpe >= 1.0:
        score += 2
    elif sharpe >= 0.5:
        score += 1
    elif sharpe < 0.2:
        score -= 1

    if sentiment >= 0.3:
        score += 1
    elif sentiment <= -0.3:
        score -= 1

    if horizon == "LP":
        if regime == "bull":
            score += 1
        elif regime == "bear":
            score -= 1

    if score >= 3:
        return "ALTA"
    if score >= 1:
        return "MEDIA"
    return "BAJA"


def _narrative_lp(ticker: str, quant: dict, tech: dict, sent: SentimentSummary, macro: MacroSnapshot, conviction: str) -> str:
    parts = [
        f"{ticker} — horizonte LARGO PLAZO. Convicción {conviction}.",
        f"La tesis CAPM/Markowitz sostiene un Sharpe de {quant.get('sharpe', 0):.2f} con beta {quant.get('beta', 0):.2f} "
        f"(retorno esperado CAPM {quant.get('expected_return_capm', 0)*100:+.1f}%, alfa de Jensen {quant.get('alpha_jensen', 0)*100:+.1f}%).",
    ]
    if conviction == "ALTA":
        parts.append("Cumple el filtro de calidad (β razonable + Sharpe elevado) y aparece en el óptimo de la frontera eficiente.")
    elif conviction == "MEDIA":
        parts.append("Pasa los filtros mínimos pero no lidera el ranking de Sharpe — es un complemento de la cartera núcleo.")

    if sent.n_headlines > 0:
        if sent.score > 0.3:
            parts.append(f"El flujo de noticias es positivo (sentiment {sent.score:+.2f} sobre {sent.n_headlines} titulares).")
        elif sent.score < -0.3:
            parts.append(f"⚠️ Flujo de noticias negativo (sentiment {sent.score:+.2f}) — monitorear de cerca.")
        else:
            parts.append(f"Flujo de noticias neutral ({sent.n_headlines} titulares).")

    if macro.regime == "bull":
        parts.append(f"Régimen de mercado BULL ({macro.regime_reason}) — viento de cola para posiciones largas.")
    elif macro.regime == "bear":
        parts.append(f"Régimen BEAR ({macro.regime_reason}) — reducir tamaño o esperar confirmación.")
    else:
        parts.append(f"Régimen LATERAL — la convicción debe venir del alfa idiosincrático, no del beta de mercado.")

    # Contexto sectorial breve (si aplica)
    if "oil" in ticker.lower() or ticker in {"XOM", "CVX", "PBR", "SLB", "SHEL", "TTE", "VIST", "YPF", "PAM"}:
        oil = macro.changes_1m.get("oil_wti")
        if oil is not None:
            parts.append(f"Contexto sectorial: WTI {oil*100:+.1f}% en el último mes.")
    if ticker in {"GOLD", "NEM", "RIO", "VALE", "FCX"}:
        gold = macro.changes_1m.get("gold")
        if gold is not None:
            parts.append(f"Contexto sectorial: oro {gold*100:+.1f}% en el último mes.")

    return " ".join(parts)


def _narrative_cp(ticker: str, quant: dict, tech: dict, sent: SentimentSummary, macro: MacroSnapshot, conviction: str) -> str:
    parts = [
        f"{ticker} — horizonte CORTO PLAZO. Convicción {conviction}.",
        f"Setup técnico: RSI {tech.get('rsi', 0):.0f}, momentum 1m {tech.get('ret_1m', 0)*100:+.1f}%, momentum 3m {tech.get('ret_3m', 0)*100:+.1f}%.",
    ]
    rsi = tech.get("rsi", 50) or 50
    if rsi < 35:
        parts.append("RSI en sobreventa → setup de rebote táctico.")
    elif rsi > 70:
        parts.append("⚠️ RSI en sobrecompra → riesgo de reversión a la media.")

    stop = tech.get("stop_loss_atr")
    price = tech.get("price")
    if stop and price:
        risk_pct = (price - stop) / price * 100
        parts.append(f"Stop loss ATR en ${stop} (riesgo ≈ {risk_pct:.1f}% desde entrada).")

    if sent.score < -0.3:
        parts.append("⚠️ Sentiment negativo puede invalidar el rebote — reducir tamaño.")
    elif sent.score > 0.3:
        parts.append("Sentiment de noticias refuerza el setup.")

    if macro.regime == "sideways":
        parts.append("Régimen lateral favorece mean-reversion de corto plazo.")
    elif macro.regime == "bear":
        parts.append("⚠️ En bear market los rebotes tienden a fallar: operar con tamaño reducido.")

    return " ".join(parts)


ENERGY_TICKERS = {"XOM", "CVX", "PBR", "SLB", "SHEL", "TTE", "VIST", "YPF", "PAM",
                  "PAMP.BA", "TGS", "EDN"}
MATERIALS_TICKERS = {"RIO", "VALE", "GOLD", "NEM", "FCX", "ALTM", "LAC", "SQM", "ALUA.BA"}
TECH_TICKERS = {"NVDA", "AMD", "MSFT", "GOOGL", "AAPL", "META", "TSLA", "TSM", "ASML", "PLTR"}
EM_TICKERS = {"PBR", "VIST", "YPF", "VALE", "MELI", "DESP", "GGAL", "BMA", "TGS", "EDN",
              "PAMP.BA", "ALUA.BA", "IRS"}


def _key_risks(ticker: str, tech: dict, sent: SentimentSummary, macro: MacroSnapshot) -> list[str]:
    """
    Detector de riesgos macro sensible al sector del activo.

    Reglas:
    - Sentiment negativo/positivo: si |score| ≥ 0.25 (threshold bajado).
    - Régimen bear siempre se marca.
    - VIX > 20 (no 25) ya es "nerviosismo elevado".
    - Dólar +2% 1m (antes 3%) golpea exportadoras USA.
    - WTI >+8% o <-8% 1m impacta energía según dirección.
    - Oro -5% 1m impacta mineras / refugio.
    - 10Y +30bps 1m golpea duration (growth/tech).
    - EM tickers + DXY fuerte = combo peligroso.
    """
    risks: list[str] = []

    if sent.score < -0.25 and sent.n_headlines >= 2:
        risks.append(f"Flujo de noticias negativo (sentiment {sent.score:+.2f}, n={sent.n_headlines}).")

    if macro.regime == "bear":
        risks.append("Régimen bajista del S&P500 — alta probabilidad de whipsaws y rebotes fallidos.")

    vix = macro.prices.get("vix")
    if vix is not None:
        if vix > 25:
            risks.append(f"VIX muy elevado ({vix:.0f}) → ATR subestima el riesgo real; considerar stops más anchos.")
        elif vix > 20:
            risks.append(f"VIX elevado ({vix:.0f}) → volatilidad en alza, reducir tamaño.")

    dxy_chg = macro.changes_1m.get("dxy")
    if dxy_chg is not None:
        if dxy_chg > 0.02 and ticker not in {"SPY", "QQQ"}:
            risks.append(f"Dólar fuerte ({dxy_chg*100:+.1f}% 1m) comprime EPS de exportadoras USA y activos EM.")
        if dxy_chg > 0.015 and ticker in EM_TICKERS:
            risks.append("Activo EM + DXY fuerte: combo históricamente negativo para emerging.")

    wti_chg = macro.changes_1m.get("oil_wti")
    if wti_chg is not None and ticker in ENERGY_TICKERS:
        if wti_chg > 0.08:
            risks.append(f"WTI {wti_chg*100:+.1f}% 1m: precio elevado favorece energía pero riesgo de demand destruction.")
        elif wti_chg < -0.08:
            risks.append(f"WTI {wti_chg*100:+.1f}% 1m: caída fuerte impacta directamente la tesis.")

    gold_chg = macro.changes_1m.get("gold")
    if gold_chg is not None and ticker in MATERIALS_TICKERS:
        if gold_chg < -0.05:
            risks.append(f"Oro {gold_chg*100:+.1f}% 1m: rotación desde refugio — revisar correlación del trade.")

    y10 = macro.prices.get("us10y")
    if y10 is not None and ticker in TECH_TICKERS and y10 > 4.5:
        risks.append(f"Yield 10Y US en {y10:.2f}% → duration alta castigada, presión sobre valuaciones growth.")

    if not risks:
        risks.append("Sin riesgos macro mayores detectados; el riesgo principal es idiosincrático del activo.")
    return risks


def build_trade_thesis(
    *,
    ticker: str,
    horizon: str,
    quant_row: pd.Series,
    tech_row: pd.Series,
    sentiment: SentimentSummary,
    macro: MacroSnapshot,
    weight_target: float,
    capital: float,
) -> TradeThesis:
    """
    Construye la tesis completa para un trade.

    Args:
        quant_row: fila del DataFrame con métricas CAPM (mu, sigma, beta, sharpe, alpha...).
        tech_row: fila con métricas técnicas (price, rsi, atr, stop_loss_atr, ret_1m...).
        sentiment: resumen de noticias del activo.
        macro: snapshot macro global.
        weight_target: % del sleeve asignado.
        capital: capital total del paper account (para calcular el USD real).
    """
    q = {
        "mu_anual": float(quant_row.get("mu_anual", 0) or 0),
        "sigma_anual": float(quant_row.get("sigma_anual", 0) or 0),
        "beta": float(quant_row.get("beta", 0) or 0),
        "alpha_jensen": float(quant_row.get("alpha_jensen", 0) or 0),
        "sharpe": float(quant_row.get("sharpe", 0) or 0),
        "expected_return_capm": float(quant_row.get("expected_return_capm", 0) or 0),
    }
    t = {
        "price": float(tech_row.get("price", 0) or 0),
        "rsi": float(tech_row.get("rsi", 0) or 0),
        "atr": float(tech_row.get("atr", 0) or 0),
        "stop_loss_atr": float(tech_row.get("stop_loss_atr", 0) or 0),
        "ret_1m": float(tech_row.get("ret_1m", 0) or 0),
        "ret_3m": float(tech_row.get("ret_3m", 0) or 0),
        "dist_52w_high": float(tech_row.get("dist_52w_high", 0) or 0),
    }

    _sleeve_weight = {
        "LP": PARAMS.weight_long_term,
        "CP": PARAMS.weight_short_term,
        "DERIV": PARAMS.weight_options,
        "HEDGE": PARAMS.weight_options,
    }.get(horizon, PARAMS.weight_short_term)
    sleeve_cap = capital * _sleeve_weight
    dollars_allocated = sleeve_cap * weight_target
    max_loss_usd = 0.0
    if t["price"] > 0 and t["stop_loss_atr"] > 0:
        risk_per_share_pct = (t["price"] - t["stop_loss_atr"]) / t["price"]
        max_loss_usd = dollars_allocated * risk_per_share_pct

    risk = {
        "dollars_allocated": round(dollars_allocated, 2),
        "weight_of_total_portfolio": round(weight_target * (PARAMS.weight_long_term if horizon == "LP" else PARAMS.weight_short_term), 4),
        "max_loss_usd_if_stop_hit": round(max_loss_usd, 2),
        "max_loss_pct_of_capital": round(max_loss_usd / capital, 4) if capital > 0 else 0,
        "stop_loss": t["stop_loss_atr"] or None,
    }

    fundamental = {
        "sentiment_score": sentiment.score,
        "n_headlines": sentiment.n_headlines,
        "positive": sentiment.positive_count,
        "negative": sentiment.negative_count,
        "sample_titles": sentiment.sample_titles,
    }
    macro_block = {
        "regime": macro.regime,
        "regime_reason": macro.regime_reason,
        "vix": macro.prices.get("vix"),
        "oil_wti": macro.prices.get("oil_wti"),
        "dxy_1m": macro.changes_1m.get("dxy"),
        "gold_1m": macro.changes_1m.get("gold"),
    }

    conviction = _conviction_from_scores(q["sharpe"], sentiment.score, macro.regime, horizon)

    narrative_fn = _narrative_lp if horizon == "LP" else _narrative_cp
    thesis_text = narrative_fn(ticker, q, t, sentiment, macro, conviction)

    return TradeThesis(
        ticker=ticker,
        horizon=horizon,
        conviction=conviction,
        quant=q,
        technical=t,
        fundamental=fundamental,
        macro=macro_block,
        risk=risk,
        thesis_text=thesis_text,
        key_risks=_key_risks(ticker, t, sentiment, macro),
    )
