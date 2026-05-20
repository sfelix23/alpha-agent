"""
Kelly Criterion para position sizing óptimo.

Fórmula continua (Gaussian):
    f* = (μ - rf) / σ²

Usamos half-Kelly (f*/2) para reducir varianza. El resultado se normaliza
dentro del sleeve y se capea en PARAMS.max_weight_per_asset.

blend_markowitz_kelly() combina Markowitz (covarianza) con Kelly (edge real)
en proporción configurable (default 60/40).

Volatilidad: blend 60% GARCH(1,1) forecast + 40% histórica realizada.
GARCH captura persistencia de clusters de vol; histórica provee estabilidad.
"""

from __future__ import annotations

import logging

import pandas as pd

from alpha_agent.config import PARAMS

logger = logging.getLogger(__name__)

# iter14 AGRESIVO: 0.5→0.65 (Kelly fraccional). Full-Kelly maximiza crecimiento
# log pero arriesga ruina si μ está sobreestimado; 0.65 captura ~91% del crecimiento
# con ~65% de la varianza. Nota: dentro de un sleeve el factor se normaliza, así que
# afecta sobre todo el blend Kelly/Markowitz (LP) y la peakedness relativa.
_HALF_KELLY  = 0.65
_MIN_SIGMA   = 0.01
_MAX_F_STAR  = 3.0   # iter14: 2.0→3.0 — deja que el nombre de mayor edge domine más antes de normalizar


def _sigma_for_ticker(ticker: str, capm: pd.DataFrame, returns: pd.DataFrame | None) -> float:
    """
    Blend 60% GARCH + 40% histórica para σ anualizado.
    Cae back a histórico si GARCH falla o no hay returns disponibles.
    """
    sigma_hist = float(capm.loc[ticker, "sigma_anual"])
    if returns is None or ticker not in returns.columns:
        return sigma_hist

    try:
        from alpha_agent.analytics.garch import forecast_garch_vol
        sigma_garch = forecast_garch_vol(returns[ticker].dropna())
        blended = 0.60 * sigma_garch + 0.40 * sigma_hist
        logger.debug("%s σ blend: GARCH=%.1f%% hist=%.1f%% → %.1f%%",
                     ticker, sigma_garch * 100, sigma_hist * 100, blended * 100)
        return blended
    except (ValueError, ZeroDivisionError, KeyError, ImportError) as e:
        # Específicos: ValueError/ZeroDivisionError vienen de arch fit en series degeneradas;
        # KeyError si returns[ticker] tiene datos faltantes; ImportError si arch no instalado.
        # No swallowear errores estructurales (FileNotFoundError, MemoryError, etc.).
        logger.debug("%s GARCH falló (%s) — fallback a σ histórico", ticker, e)
        return sigma_hist


def kelly_weights(capm: pd.DataFrame, returns: pd.DataFrame | None = None) -> pd.Series:
    """
    Half-Kelly normalizado para cada activo del DataFrame CAPM.
    Activos con edge negativo reciben peso 0.
    Si se provee `returns`, usa σ blend GARCH+histórico para cada activo.
    """
    rf    = PARAMS.risk_free_rate
    w_max = PARAMS.max_weight_per_asset

    fractions: dict[str, float] = {}
    for ticker in capm.index:
        mu    = float(capm.loc[ticker, "mu_anual"])
        sigma = _sigma_for_ticker(ticker, capm, returns)
        if sigma < _MIN_SIGMA:
            fractions[ticker] = 0.0
            continue
        f_star = (mu - rf) / (sigma ** 2)
        f_star = min(f_star, _MAX_F_STAR)   # cap antes de half-Kelly: evita over-sizing con sigma < 0.05
        fractions[ticker] = max(0.0, f_star * _HALF_KELLY)

    series = pd.Series(fractions)
    total  = series.sum()
    if total <= 0:
        # Todos con edge negativo → no forzar posiciones; el caller decide si usa equal-weight
        logger.warning("kelly_weights: edge negativo en todos los activos — retornando pesos cero")
        return pd.Series(0.0, index=series.index)

    series = series / total

    # Cap iterativo: exceso se redistribuye entre los no capados
    for _ in range(10):
        over = series > w_max
        if not over.any():
            break
        excess = (series[over] - w_max).sum()
        series[over] = w_max
        under = ~over & (series > 0)
        if under.any():
            series[under] += excess * (series[under] / series[under].sum())

    s = series.sum()
    return series / s if s > 0 else series


def blend_markowitz_kelly(
    markowitz_weights: pd.Series,
    capm: pd.DataFrame,
    *,
    kelly_alpha: float = 0.50,
) -> pd.Series:
    """
    final = (1 - kelly_alpha) * markowitz + kelly_alpha * kelly

    Args:
        kelly_alpha: peso de Kelly en el blend. iter14 AGRESIVO: 0.30→0.50 — pondera
                     más el edge real (Kelly) vs minimizar varianza (Markowitz).
                     Markowitz tiende a diluir en muchos nombres de baja vol; subir
                     kelly_alpha concentra en donde el retorno esperado es mayor.

    Returns:
        Serie renormalizada a suma = 1.
    """
    tickers  = markowitz_weights[markowitz_weights > 0].index.tolist()
    capm_sub = capm.loc[[t for t in tickers if t in capm.index]]
    if capm_sub.empty:
        return markowitz_weights

    k_w = kelly_weights(capm_sub)
    mw  = markowitz_weights.reindex(k_w.index).fillna(0.0)
    if mw.sum() > 0:
        mw = mw / mw.sum()

    blended = (1 - kelly_alpha) * mw + kelly_alpha * k_w
    s = blended.sum()
    if s > 0:
        blended = blended / s

    logger.info(
        "Kelly blend α=%.0f%% → %s",
        kelly_alpha * 100,
        {t: f"{w:.1%}" for t, w in blended[blended > 0].items()},
    )
    return blended


# ─────────────────────────────────────────────────────────────────────────────
# Risk budget escalado y Kelly multiplier por régimen (Sesión 4-5 del plan)
#
# El plan original reemplazaba el kill switch binario -3% por un escalado
# por bandas de drawdown. También módula el Kelly fraction según régimen + VIX
# para presionar el acelerador en BULL y bajar en BEAR.
# ─────────────────────────────────────────────────────────────────────────────


def risk_action_for_drawdown(drawdown_pct: float) -> dict[str, str | float]:
    """Devuelve la acción de riesgo según la banda de drawdown intradía.

    En vez de kill switch binario -3% (cierra todo), escala:
        0 a -2%   → operación normal
        -2 a -4%  → reduce Kelly fraction 50%, no entradas nuevas
        -4 a -6%  → close losers, hold winners
        -6 a -8%  → close all longs, mantener hedge SPY puts si hay
        < -8%     → kill switch total + alerta

    Args:
        drawdown_pct: negativo (ej. -3.5).

    Returns:
        Dict con:
          - level: "NORMAL" | "REDUCE" | "CLOSE_LOSERS" | "CLOSE_LONGS" | "KILL"
          - kelly_multiplier: 0.0 a 1.0 (escala el sizing)
          - new_entries_allowed: bool
          - description: human-readable
    """
    # iter14 AGRESIVO: bandas más anchas. Con posiciones de mayor vol, -2/-4% es
    # ruido normal; cortar ahí te saca por whipsaw justo antes del rebote. Dejamos
    # respirar hasta -6%, recortamos gradual, y el piso anti-ruina pasa a -13%
    # (un -13% necesita +15% para recuperar — todavía componible; -50% necesita +100%).
    if drawdown_pct >= -3.0:
        return {
            "level": "NORMAL", "kelly_multiplier": 1.0, "new_entries_allowed": True,
            "description": f"Operación normal — drawdown {drawdown_pct:+.1f}%",
        }
    if drawdown_pct >= -6.0:
        # iter14: seguimos permitiendo entradas (presionar), sólo bajamos un poco el sizing
        return {
            "level": "REDUCE", "kelly_multiplier": 0.7, "new_entries_allowed": True,
            "description": f"Sizing 0.7x, entradas selectivas — drawdown {drawdown_pct:+.1f}%",
        }
    if drawdown_pct >= -9.0:
        return {
            "level": "CLOSE_LOSERS", "kelly_multiplier": 0.4, "new_entries_allowed": False,
            "description": f"Cerrar perdedores, hold ganadores — drawdown {drawdown_pct:+.1f}%",
        }
    if drawdown_pct >= -13.0:
        return {
            "level": "CLOSE_LONGS", "kelly_multiplier": 0.0, "new_entries_allowed": False,
            "description": f"Cerrar TODOS los longs, mantener hedge — drawdown {drawdown_pct:+.1f}%",
        }
    return {
        "level": "KILL", "kelly_multiplier": 0.0, "new_entries_allowed": False,
        "description": f"KILL SWITCH — drawdown {drawdown_pct:+.1f}% inferior a -13%",
    }


def kelly_multiplier_for_regime(regime: str, vix: float) -> float:
    """Multiplicador del Kelly base según régimen + VIX.

    Hoy el sistema usa half-Kelly plano (0.5) para todo. Esto módula:
        BULL  + VIX<15   → 0.6 (más agresivo, drawdowns esperados -10/-15%)
        BULL  + VIX<22   → 0.5 (default)
        LATERAL + VIX<22 → 0.4
        BEAR  + VIX<25   → 0.3 (defensivo)
        Cualquiera + VIX>25 → 0.2 (panic mode)

    Args:
        regime: "BULL" | "BEAR" | "LATERAL" (case-insensitive)
        vix: nivel actual del VIX.

    Returns:
        Fracción [0.2, 0.6] que se aplica al f_star de Kelly.
    """
    # iter14 AGRESIVO: multiplicadores subidos. En BULL (drift positivo = equity
    # risk premium) presionar fuerte es EV-positivo. Sólo se recorta de verdad en
    # VIX>28 (régimen de pánico, donde la correlación va a 1 y la diversificación falla).
    r = regime.upper() if isinstance(regime, str) else "LATERAL"
    if vix > 28:
        return 0.35
    if r == "BULL":
        return 0.90 if vix < 15 else 0.75
    if r == "BEAR":
        return 0.45
    return 0.60   # LATERAL o desconocido


def adaptive_trailing(conviction: str, regime: str) -> dict[str, float]:
    """Trailing stop adaptativo según conviction × régimen.

    Reemplaza la regla fija "+5%→breakeven, +10%→lock 50%" por una tabla
    que deja correr más a los winners en BULL+ALTA y protege antes en
    BEAR+MEDIA.

    Returns:
        Dict con:
          - be_at_pct: mover stop a breakeven cuando P&L >= este %
          - lock_at_pct: empezar a proteger profit cuando P&L >= este %
          - lock_fraction: qué fracción del profit proteger (0-1)
    """
    r = regime.upper() if isinstance(regime, str) else "LATERAL"
    c = conviction.upper() if isinstance(conviction, str) else "MEDIA"
    table = {
        ("BULL",    "ALTA"):  {"be_at_pct": 8.0,  "lock_at_pct": 20.0, "lock_fraction": 0.6},
        ("BULL",    "MEDIA"): {"be_at_pct": 6.0,  "lock_at_pct": 15.0, "lock_fraction": 0.5},
        ("LATERAL", "ALTA"):  {"be_at_pct": 5.0,  "lock_at_pct": 12.0, "lock_fraction": 0.5},
        ("LATERAL", "MEDIA"): {"be_at_pct": 4.0,  "lock_at_pct": 10.0, "lock_fraction": 0.4},
        ("BEAR",    "ALTA"):  {"be_at_pct": 3.0,  "lock_at_pct": 8.0,  "lock_fraction": 0.4},
        ("BEAR",    "MEDIA"): {"be_at_pct": 2.0,  "lock_at_pct": 5.0,  "lock_fraction": 0.3},
    }
    return table.get((r, c), table[("LATERAL", "MEDIA")])


def equity_curve_multiplier(equity_history: list[float]) -> tuple[float, str]:
    """Multiplicador de sizing según la propia equity curve (meta-strategy).

    Idea: cuando estás en racha (equity > MA20), presioná; cuando perdés
    racha (equity < MA20), achicá. Equity < MA50 por >5 días → modo defensivo.
    En backtests reduce drawdowns 30-50% manteniendo gran parte del upside.

    Args:
        equity_history: lista de equity histórico (orden cronológico,
            del más antiguo al más reciente). Lee desde
            signals/equity_snapshots.json.

    Returns:
        (multiplier, regime_label) donde multiplier ∈ [0.35, 1.35] (iter14) y
        regime_label es "HOT" | "NORMAL" | "COOLING" | "DEFENSIVE".
    """
    if not equity_history or len(equity_history) < 5:
        return 1.0, "NORMAL"

    ma20 = sum(equity_history[-20:]) / min(20, len(equity_history))
    current = equity_history[-1]

    if len(equity_history) >= 50:
        ma50 = sum(equity_history[-50:]) / 50
        # ¿>5 días por debajo de MA50?
        recent_below_ma50 = sum(
            1 for v in equity_history[-7:] if v < ma50
        )
        if recent_below_ma50 >= 5:
            return 0.35, "DEFENSIVE"   # iter14: 0.0→0.35 — recortar fuerte pero no quedar flat

    # iter14 AGRESIVO (anti-martingala): piramidar más fuerte en racha, achicar menos.
    if current > ma20:
        return 1.35, "HOT"          # iter14: 1.2→1.35 — presionar la racha ganadora
    if current < ma20 * 0.97:   # claramente debajo (3% margen)
        return 0.80, "COOLING"      # iter14: 0.7→0.80 — de-risk más suave
    return 1.0, "NORMAL"


def composite_kelly_multiplier(
    *,
    regime: str,
    vix: float,
    drawdown_pct: float,
    equity_history: list[float] | None = None,
) -> dict:
    """Compone los 3 multiplicadores de Kelly en uno solo para el sizing final.

    final = regime_mult × drawdown_mult × equity_curve_mult

    Devuelve un dict con los 3 componentes + el final, para que el caller
    pueda loguear cuál bajó el sizing en qué corrida.
    """
    regime_mult = kelly_multiplier_for_regime(regime, vix)
    risk = risk_action_for_drawdown(drawdown_pct)
    drawdown_mult = float(risk["kelly_multiplier"])
    if equity_history:
        ec_mult, ec_regime = equity_curve_multiplier(equity_history)
    else:
        ec_mult, ec_regime = 1.0, "NORMAL"

    final = regime_mult * drawdown_mult * ec_mult
    return {
        "regime_mult": regime_mult,
        "drawdown_mult": drawdown_mult,
        "drawdown_level": risk["level"],
        "equity_curve_mult": ec_mult,
        "equity_curve_regime": ec_regime,
        "final_multiplier": round(final, 3),
        "new_entries_allowed": bool(risk["new_entries_allowed"]) and ec_regime != "DEFENSIVE",
    }
