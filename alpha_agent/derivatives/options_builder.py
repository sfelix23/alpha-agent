"""
Constructor de señales de opciones.

Sin depender de una chain real (no la tenemos a esta altura del pipeline),
construimos un "contrato sintético" con:
    - strike estimado según el delta objetivo (ATM ≈ 0.50, OTM ≈ 0.30, ITM ≈ 0.70).
      Aproximación simple: strike ≈ precio * (1 ± k*IV*sqrt(T)) donde k depende del delta.
    - expiry estimado: fecha calendario a `min_days_to_expiry + 14` días.
    - prima estimada con Black-Scholes simplificado usando σ anual = sigma del activo
      (o 25% default si no se conoce). Sin skew de volatilidad.
    - delta / theta / IV aproximados.

El objetivo NO es pricing exacto sino poder:
    (a) filtrar candidatos por costo (prima < max_single_option_premium),
    (b) dimensionar cuántos contratos entran en el bucket,
    (c) darle al trader_agent un objeto con todos los campos que Alpaca
        va a necesitar cuando pidamos la chain real antes de enviar la orden.

Al momento de ejecutar (en el trader_agent) se pide la chain real al broker
y se elige el contrato más cercano al strike/expiry indicados acá.
"""

from __future__ import annotations

import logging
import math
from datetime import date, timedelta

import pandas as pd

from alpha_agent.config import PARAMS
from alpha_agent.macro.macro_context import MacroSnapshot
from alpha_agent.news import fetch_ticker_news, summarize_sentiment
from alpha_agent.reasoning import build_trade_thesis
from alpha_agent.reporting.signals import Signal

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Pricing aproximado (Black-Scholes) — solo para sizing del bucket
# ─────────────────────────────────────────────────────────────────────────────
def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_call(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0:
        return max(0.0, S - K)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)


def _bs_put(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0:
        return max(0.0, K - S)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def _bs_delta(S: float, K: float, T: float, r: float, sigma: float, kind: str) -> float:
    if T <= 0 or sigma <= 0:
        return 0.5 if kind == "call" else -0.5
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    return _norm_cdf(d1) if kind == "call" else _norm_cdf(d1) - 1.0


def _strike_for_delta(S: float, T: float, sigma: float, r: float, target_delta: float, kind: str) -> float:
    """
    Encuentra un strike que produzca aproximadamente el delta objetivo.
    Hace búsqueda grid simple en ±30% del spot.
    """
    best_k = S
    best_diff = 1.0
    for pct in range(-30, 31, 1):
        k = S * (1 + pct / 100)
        d = _bs_delta(S, k, T, r, sigma, kind)
        ref = target_delta if kind == "call" else -target_delta
        diff = abs(d - ref)
        if diff < best_diff:
            best_diff = diff
            best_k = k
    return round(best_k, 2)


# ─────────────────────────────────────────────────────────────────────────────
# Builder principal
# ─────────────────────────────────────────────────────────────────────────────
def _build_option_signal(
    *,
    ticker: str,
    direction: str,            # "BUY_PUT" | "BUY_CALL"
    spot: float,
    sigma_anual: float,
    quant_row: pd.Series,
    tech_row: pd.Series,
    macro: MacroSnapshot,
    capital: float,
    weight_target: float,
    target_delta: float,
) -> Signal | None:
    """
    Construye una Signal de opción con strike/expiry/prima estimada.
    Devuelve None si la prima excede el cap.
    """
    if spot <= 0 or sigma_anual <= 0:
        logger.warning("Skipping %s: spot/sigma inválidos", ticker)
        return None

    days_to_expiry = (PARAMS.min_days_to_expiry + PARAMS.max_days_to_expiry) // 2
    T = days_to_expiry / 365.0
    r = PARAMS.risk_free_rate

    kind = "call" if direction == "BUY_CALL" else "put"

    # Intentamos el delta objetivo; si la prima excede el cap, iteramos a
    # deltas más OTM (0.6x, 0.36x, 0.22x) hasta encontrar algo que entre.
    delta_try = target_delta
    contract_cost = None
    strike = None
    premium = None
    for step in range(PARAMS.otm_fallback_steps + 1):
        strike = _strike_for_delta(spot, T, sigma_anual, r, delta_try, kind)
        if kind == "call":
            premium = _bs_call(spot, strike, T, r, sigma_anual)
        else:
            premium = _bs_put(spot, strike, T, r, sigma_anual)
        contract_cost = premium * 100
        if contract_cost <= PARAMS.max_single_option_premium:
            if step > 0:
                logger.info(
                    "  ↺ %s %s: delta bajado a %.2f (prima $%.0f) por cap",
                    ticker, direction, delta_try, contract_cost,
                )
            break
        delta_try = delta_try * 0.6
    else:
        logger.info(
            "  ⊘ %s %s descartado: ni con delta %.2f el contrato entra en cap $%.0f (último intento $%.0f)",
            ticker, direction, delta_try, PARAMS.max_single_option_premium, contract_cost,
        )
        return None

    if kind == "call":
        breakeven = round(strike + premium, 2)
    else:
        breakeven = round(strike - premium, 2)

    expiry_date = date.today() + timedelta(days=days_to_expiry)

    # Sentiment + tesis (reutilizamos el motor de reasoning con horizonte DERIV)
    try:
        sentiment = summarize_sentiment(fetch_ticker_news(ticker))
    except Exception:
        from alpha_agent.news.sentiment import SentimentSummary
        sentiment = SentimentSummary(score=0.0, n_headlines=0, positive_count=0, negative_count=0, sample_titles=[])

    thesis = build_trade_thesis(
        ticker=ticker,
        horizon="DERIV",
        quant_row=quant_row,
        tech_row=tech_row,
        sentiment=sentiment,
        macro=macro,
        weight_target=weight_target,
        capital=capital,
    )
    # Sobreescribir narrativa con el ángulo de opciones
    bias = "bajista" if kind == "put" else "alcista"
    thesis.thesis_text = (
        f"{ticker} — apuesta {bias} vía {kind.upper()} (riesgo limitado a la prima). "
        f"Strike ${strike}, expiry {expiry_date.isoformat()} (~{days_to_expiry}d), "
        f"prima estimada ${premium:.2f}/share = ${contract_cost:.0f}/contrato. "
        f"Breakeven ${breakeven}. Delta objetivo {target_delta:.2f}. "
        f"Backup: {thesis.thesis_text}"
    )

    option_block = {
        "type": kind,                 # "call" | "put"
        "strike": strike,
        "expiry": expiry_date.isoformat(),
        "days_to_expiry": days_to_expiry,
        "premium_per_share_est": round(premium, 2),
        "contract_cost_est": round(contract_cost, 2),
        "breakeven": breakeven,
        "delta_est": round(target_delta if kind == "call" else -target_delta, 2),
        "sigma_used": round(sigma_anual, 3),
        "max_loss_usd": round(contract_cost, 2),   # riesgo máximo = prima
    }

    return Signal(
        ticker=ticker,
        side=direction,
        horizon="DERIV",
        price=round(spot, 2),
        stop_loss=None,                # no hay stop en opciones long — el loss max ya está capado
        take_profit=None,
        weight_target=round(float(weight_target), 4),
        thesis=thesis.to_dict(),
        option=option_block,
    )


def build_directional_options_signals(
    *,
    bullish_candidates: pd.DataFrame | None,
    bearish_candidates: pd.DataFrame | None,
    macro: MacroSnapshot,
    capital: float,
) -> list[Signal]:
    """
    Combina top bullish y top bearish en el options_book.
    Respeta:
        - top_n_bearish
        - max_single_option_premium
        - Reparto de peso: 50/50 entre long calls y long puts si hay ambos;
          100% del lado que haya si solo hay uno.

    Args:
        bullish_candidates: DataFrame ordenado con columnas incluyendo
            sigma_anual, price (o close), rsi, ret_1m, alpha_jensen, sharpe, beta.
        bearish_candidates: lo mismo para bajistas.

    Returns:
        Lista de Signals. Puede ser vacía si nada pasa los filtros.
    """
    if not PARAMS.enable_options:
        return []

    signals: list[Signal] = []

    def _row_spot(row: pd.Series) -> float:
        # la columna "price" viene del bloque technical; si no está, usar close
        return float(row.get("price", 0) or 0)

    def _row_sigma(row: pd.Series) -> float:
        s = float(row.get("sigma_anual", 0) or 0)
        return s if s > 0 else 0.25   # fallback 25% anual

    # ── PUTS direccionales (bearish bets) ───────────────────────────────
    if bearish_candidates is not None and not bearish_candidates.empty:
        top_bear = bearish_candidates.head(PARAMS.top_n_bearish)
        # peso por señal (repartir el lado bear dentro del sleeve)
        w_per = 1.0 / max(len(top_bear), 1) * 0.5  # 50% del sleeve va a puts
        for ticker, row in top_bear.iterrows():
            sig = _build_option_signal(
                ticker=ticker,
                direction="BUY_PUT",
                spot=_row_spot(row),
                sigma_anual=_row_sigma(row),
                quant_row=row,
                tech_row=row,
                macro=macro,
                capital=capital,
                weight_target=w_per,
                target_delta=PARAMS.target_delta_directional,
            )
            if sig:
                signals.append(sig)

    # ── CALLS direccionales (top bullish ideas que sobraron del LP) ─────
    # Para no duplicar el long equity, tomamos los top 1-2 candidates con el
    # mayor alpha_jensen del pool LP que NO entraron al portfolio Markowitz.
    if bullish_candidates is not None and not bullish_candidates.empty:
        top_bull = bullish_candidates.head(2)
        w_per = 1.0 / max(len(top_bull), 1) * 0.5
        for ticker, row in top_bull.iterrows():
            sig = _build_option_signal(
                ticker=ticker,
                direction="BUY_CALL",
                spot=_row_spot(row),
                sigma_anual=_row_sigma(row),
                quant_row=row,
                tech_row=row,
                macro=macro,
                capital=capital,
                weight_target=w_per,
                target_delta=PARAMS.target_delta_directional,
            )
            if sig:
                signals.append(sig)

    # Si solo hay un lado, reasignar pesos para que sumen al 100% del sleeve
    if signals:
        total_w = sum(s.weight_target for s in signals)
        if total_w > 0 and total_w < 0.99:
            scale = 1.0 / total_w
            for s in signals:
                s.weight_target = round(s.weight_target * scale, 4)

    return signals


def build_hedge_signals(
    *,
    spy_spot: float,
    spy_sigma: float,
    macro: MacroSnapshot,
    capital: float,
) -> list[Signal]:
    """
    Hedge layer: cuando el régimen es bear o el VIX > 22, comprar puts SPY
    como seguro del libro long. Delta objetivo ≈ 0.30 (OTM, prima baja).

    Devuelve lista vacía si el entorno no justifica hedge o si el costo excede
    el límite de hedge allocation.
    """
    if not PARAMS.enable_options:
        return []

    vix = macro.prices.get("vix", 15.0) or 15.0
    need_hedge = (macro.regime == "bear") or (vix > 22)
    if not need_hedge:
        logger.info("Hedge layer: no necesario (régimen=%s, VIX=%.1f).", macro.regime, vix)
        return []

    logger.info("Hedge layer: ACTIVO (régimen=%s, VIX=%.1f).", macro.regime, vix)

    days = 35
    T = days / 365.0
    sigma = spy_sigma if spy_sigma > 0 else 0.18
    r = PARAMS.risk_free_rate

    max_hedge_dollars = capital * PARAMS.max_hedge_allocation

    # Fallback OTM: si el delta target es muy caro, bajamos a strikes más OTM
    delta_try = PARAMS.target_delta_hedge
    strike = premium = contract_cost = None
    for step in range(PARAMS.otm_fallback_steps + 1):
        strike = _strike_for_delta(spy_spot, T, sigma, r, delta_try, "put")
        premium = _bs_put(spy_spot, strike, T, r, sigma)
        contract_cost = premium * 100
        if contract_cost <= max_hedge_dollars:
            if step > 0:
                logger.info(
                    "Hedge SPY: delta bajado a %.2f (prima $%.0f)", delta_try, contract_cost,
                )
            break
        delta_try = delta_try * 0.6
    else:
        logger.warning(
            "Hedge layer: ni con delta %.2f la SPY put entra en cap $%.0f (prima $%.0f) — skip.",
            delta_try, max_hedge_dollars, contract_cost,
        )
        return []

    expiry_date = date.today() + timedelta(days=days)
    weight = contract_cost / (capital * PARAMS.weight_options)
    weight = min(weight, 1.0)

    thesis_dict = {
        "ticker": "SPY",
        "horizon": "HEDGE",
        "conviction": "MEDIA",
        "thesis_text": (
            f"Hedge de cartera: PUT SPY strike ${strike}, expiry {expiry_date.isoformat()}, "
            f"prima estimada ${premium:.2f}/share = ${contract_cost:.0f}/contrato. "
            f"Objetivo: mitigar drawdown del libro long si el régimen bear se confirma o "
            f"el VIX se expande. Riesgo máximo = prima (${contract_cost:.0f}). "
            f"Trigger: régimen={macro.regime}, VIX={vix:.1f}."
        ),
        "risk": {"max_loss_usd_if_stop_hit": round(contract_cost, 2)},
        "key_risks": [
            "Decaimiento theta si el mercado no se mueve en el plazo del contrato.",
            "Si el régimen cambia a bull antes del expiry, la prima se evapora.",
        ],
    }

    sig = Signal(
        ticker="SPY",
        side="BUY_PUT",
        horizon="HEDGE",
        price=round(spy_spot, 2),
        stop_loss=None,
        take_profit=None,
        weight_target=round(weight, 4),
        thesis=thesis_dict,
        option={
            "type": "put",
            "strike": strike,
            "expiry": expiry_date.isoformat(),
            "days_to_expiry": days,
            "premium_per_share_est": round(premium, 2),
            "contract_cost_est": round(contract_cost, 2),
            "breakeven": round(strike - premium, 2),
            "delta_est": -PARAMS.target_delta_hedge,
            "sigma_used": round(sigma, 3),
            "max_loss_usd": round(contract_cost, 2),
            "role": "portfolio_hedge",
        },
    )
    return [sig]
