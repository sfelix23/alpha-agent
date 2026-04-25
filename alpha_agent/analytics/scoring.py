"""
Scoring compuesto LP / CP — versión mejorada.

Pipeline LP:
  1. Filtro de calidad: beta razonable + Sharpe mínimo + tendencia alcista (EMA50).
  2. Ranking por Sharpe + alpha.
  3. Boost por rotación sectorial (sector con mejor momentum +35%).
  4. Guard de correlación y sector.

Pipeline CP:
  1. Score de momentum con confirmación de volumen y MACD.
  2. Bonus por breakout técnico (precio en máximos con volumen).
  3. Boost por rotación sectorial.
  4. Penalización si RSI > 75 (no comprar lo que ya subió demasiado).

La incorporación de volume, MACD y EMA permite capturar movimientos
con convicción institucional, no solo quant puro.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from alpha_agent.config import (
    MAX_PAIR_CORRELATION,
    MAX_SECTOR_WEIGHT_LP,
    PARAMS,
    SECTOR_MAP,
)

logger = logging.getLogger(__name__)


def _zscore(series: pd.Series) -> pd.Series:
    s = series.copy().astype(float)
    if s.std(ddof=0) == 0 or s.dropna().empty:
        return pd.Series(0.0, index=series.index)
    return (s - s.mean()) / s.std(ddof=0)


def filter_by_correlation_and_sector(
    candidates_sorted: list[str],
    returns: pd.DataFrame,
    *,
    max_pair_corr: float = MAX_PAIR_CORRELATION,
    max_sector_count: int = 2,
) -> list[str]:
    accepted: list[str] = []
    sector_count: dict[str, int] = {}
    corr_matrix = returns.corr()

    for t in candidates_sorted:
        if t not in corr_matrix.columns:
            continue
        sector = SECTOR_MAP.get(t, "Other")
        if sector_count.get(sector, 0) >= max_sector_count:
            continue
        skip = False
        for acc in accepted:
            rho = corr_matrix.loc[t, acc]
            if pd.notna(rho) and abs(rho) > max_pair_corr:
                logger.info("  ⊘ %s descartado: ρ=%.2f con %s (sector %s)", t, rho, acc, sector)
                skip = True
                break
        if skip:
            continue
        accepted.append(t)
        sector_count[sector] = sector_count.get(sector, 0) + 1

    return accepted


def compute_sector_weights(weights: pd.Series) -> dict[str, float]:
    out: dict[str, float] = {}
    for t, w in weights.items():
        sector = SECTOR_MAP.get(t, "Other")
        out[sector] = out.get(sector, 0.0) + float(w)
    return out


def _get_sector_boost(tickers: list[str]) -> dict[str, float]:
    """Carga el sector boost de rotación. Si falla, devuelve neutro (1.0)."""
    try:
        from alpha_agent.macro.sector_rotation import build_sector_boost
        return build_sector_boost(tickers)
    except Exception as exc:
        logger.debug("sector_rotation no disponible (%s)", exc)
        return {t: 1.0 for t in tickers}


def _get_earnings_soon(tickers: list[str]) -> set[str]:
    """Tickers con earnings en los próximos 3 días. Falla silenciosamente."""
    try:
        from alpha_agent.analytics.earnings_guard import get_earnings_soon
        return set(get_earnings_soon(tickers, days=3).keys())
    except Exception as exc:
        logger.debug("earnings_guard no disponible (%s)", exc)
        return set()


def _get_intraday_signals(tickers: list[str]) -> dict[str, float]:
    """Señales intraday 15 min para CP. Falla silenciosamente."""
    try:
        from alpha_agent.data.intraday import fetch_intraday_signals
        return fetch_intraday_signals(tickers)
    except Exception as exc:
        logger.debug("intraday signals no disponible (%s)", exc)
        return {t: 0.0 for t in tickers}


def _get_quality_bonus(tickers: list[str]) -> dict[str, float]:
    """
    Quality factor bonus para LP y CP.

    Usa get_fundamentals() (cached) para aplicar:
      - ROE > 15%  → +0.15  (negocio rentable)
      - Deuda/Equity < 1.0 → +0.10 (balance sólido)
      - Analyst rating >= 4 (Buy/Strong Buy) → +0.18 (consenso alcista)
      - Analyst rating <= 2 (Sell/Strong Sell) → -0.25 (consenso bajista)
      - Revenue growth > 10% YoY → +0.08

    Solo corre sobre los shortlistados (no los 49 del universo).
    Falla silenciosamente si fundamentals no disponible.
    """
    try:
        from alpha_agent.analytics.fundamental import get_fundamentals
        bonuses: dict[str, float] = {}
        for t in tickers:
            try:
                f = get_fundamentals(t)
                bonus = 0.0
                roe          = f.get("roe") or 0
                debt_eq      = f.get("debt_equity")
                rating       = f.get("analyst_rating") or 3.0
                rev_growth   = f.get("revenue_growth_yoy") or 0

                if roe > 15:
                    bonus += 0.15
                elif roe > 8:
                    bonus += 0.07

                if debt_eq is not None:
                    if debt_eq < 0.5:
                        bonus += 0.12
                    elif debt_eq < 1.0:
                        bonus += 0.08

                if rating >= 4.0:
                    bonus += 0.18
                elif rating <= 2.0:
                    bonus -= 0.25

                if rev_growth > 10:
                    bonus += 0.08

                # Upgrade/downgrade reciente de analistas (señal institucional fuerte)
                try:
                    from alpha_agent.analytics.fundamental import get_recent_upgrades
                    upg = get_recent_upgrades(t, days=7)
                    if upg.get("recent_upgrade"):
                        bonus += 0.20
                    if upg.get("recent_downgrade"):
                        bonus -= 0.20
                except Exception:
                    pass

                bonuses[t] = bonus
            except Exception:
                bonuses[t] = 0.0
        if bonuses:
            logger.info("Quality bonus: %s", {t: f"{v:+.2f}" for t, v in bonuses.items() if v != 0})
        return bonuses
    except Exception as exc:
        logger.debug("quality_bonus no disponible (%s)", exc)
        return {t: 0.0 for t in tickers}


def build_scores(
    capm: pd.DataFrame,
    technical: pd.DataFrame,
    *,
    closes: pd.DataFrame | None = None,
    regime: str = "unknown",
    prev_sentiment: dict[str, float] | None = None,
    insider_signal: dict[str, float] | None = None,
    fear_greed: int | None = None,
) -> dict[str, pd.DataFrame]:
    """
    Devuelve {'long_term': df, 'short_term': df} con rankings mejorados.
    """
    df = capm.join(technical, how="inner")

    all_tickers = df.index.tolist()
    sector_boost    = _get_sector_boost(all_tickers)
    earnings_soon   = _get_earnings_soon(all_tickers)
    intraday_scores = _get_intraday_signals(all_tickers)

    # ── LONG TERM ─────────────────────────────────────────────────────────────
    lp = df[
        (df["beta"].abs() <= PARAMS.max_beta_lp)
        & (df["sharpe"] >= PARAMS.min_sharpe_lp)
    ].copy()
    lp = lp.sort_values("sharpe", ascending=False)

    # Score base: Sharpe + alpha Jensen
    lp["score_lp"] = _zscore(lp["sharpe"]) + 0.5 * _zscore(lp["alpha_jensen"])

    # Bonus EMA50: solo activos en tendencia alcista
    if "above_ema50" in lp.columns:
        lp["score_lp"] += 0.3 * lp["above_ema50"].fillna(0)

    # Bonus MACD bullish
    if "macd_bullish" in lp.columns:
        lp["score_lp"] += 0.2 * lp["macd_bullish"].fillna(0)

    # Bonus volumen: convicción institucional
    if "vol_ratio" in lp.columns:
        vol_signal = (lp["vol_ratio"].fillna(1.0) - 1.0).clip(lower=0)
        lp["score_lp"] += 0.15 * _zscore(vol_signal)

    # Boost por rotación sectorial
    lp["score_lp"] *= lp.index.map(lambda t: sector_boost.get(t, 1.0))

    # Bull regime: bonus para beta en [1.0, 1.5] — capturar upside del mercado
    if regime.lower() == "bull" and "beta" in lp.columns:
        bull_beta_mask = (lp["beta"] >= 1.0) & (lp["beta"] <= 1.5)
        lp.loc[bull_beta_mask, "score_lp"] += 0.25

    # Insider buy bonus LP: señal institucional de conviction real
    if insider_signal:
        for t, strength in insider_signal.items():
            if t in lp.index and strength > 0:
                lp.loc[t, "score_lp"] += 0.20 * min(strength, 1.0)
                logger.info("Insider buy bonus LP %s: +%.2f", t, 0.20 * min(strength, 1.0))

    # Fear & Greed LP: extremo miedo = oportunidad, extremo codicia = precaución
    if fear_greed is not None:
        if fear_greed <= 25:       # extreme fear → contrarian long
            lp["score_lp"] *= 1.10
            logger.info("F&G %d (extreme fear) → LP scores ×1.10", fear_greed)
        elif fear_greed >= 80:     # extreme greed → reduce conviction
            lp["score_lp"] *= 0.90
            logger.info("F&G %d (extreme greed) → LP scores ×0.90", fear_greed)

    lp = lp.sort_values("score_lp", ascending=False)

    # Guard de correlación y sector
    if closes is not None and len(lp) > 1:
        returns = np.log(closes / closes.shift(1)).dropna(how="all")
        returns = returns[[c for c in lp.index if c in returns.columns]]
        ranking = lp.index.tolist()
        target = PARAMS.top_n_long_term * 2
        logger.info("Aplicando guard correlación/sector a %d candidatos LP…", len(ranking))
        kept = filter_by_correlation_and_sector(ranking, returns)[:target]
        logger.info("Shortlist LP post-guard: %s", kept)
        lp = lp.loc[kept]

    # Quality factor bonus sobre shortlist (ROE, deuda, analyst rating)
    quality_lp = _get_quality_bonus(lp.index.tolist())
    lp["quality_bonus"] = lp.index.map(lambda t: quality_lp.get(t, 0.0))
    lp["score_lp"] += lp["quality_bonus"]
    lp = lp.sort_values("score_lp", ascending=False)

    # ── SHORT TERM ────────────────────────────────────────────────────────────
    st = df.copy()

    # Score base: momentum ponderado
    rsi_signal   = (PARAMS.rsi_oversold - st["rsi"]).clip(lower=0)
    high_penalty = st["dist_52w_high"].clip(upper=0).abs()

    score_st = (
        0.40 * _zscore(st["ret_1m"].fillna(0))
        + 0.20 * _zscore(st["ret_3m"].fillna(0))
        + 0.15 * _zscore(rsi_signal)
        + 0.10 * _zscore(st["alpha_jensen"].clip(lower=0))
        - 0.05 * _zscore(-high_penalty)
    )

    # Bonus MACD: tendencia con momentum (alto impacto en CP)
    if "macd_bullish" in st.columns:
        score_st += 0.15 * st["macd_bullish"].fillna(0)

    # Bonus volumen: movimiento con convicción
    if "vol_ratio" in st.columns:
        vol_signal_st = (st["vol_ratio"].fillna(1.0) - 1.0).clip(lower=0)
        score_st += 0.15 * _zscore(vol_signal_st)

    # Bonus breakout: precio en máximos con volumen → señal de ruptura
    if "breakout" in st.columns:
        score_st += 0.20 * st["breakout"].fillna(0)

    # Bonus golden cross
    if "golden_cross" in st.columns:
        score_st += 0.10 * st["golden_cross"].fillna(0)

    # Penalización RSI sobrecomprado (no perseguir rallies tardíos)
    if "rsi" in st.columns:
        score_st -= 0.15 * (st["rsi"].fillna(50) > 75).astype(float)

    # Momentum confluence: penalizar señales contradictorias entre timeframes
    # 5d vs 1m: si el ultra-corto y el mensual se contradicen → -0.15
    if "ret_5d" in st.columns and "ret_1m" in st.columns:
        conflicting_5d_1m = (
            (st["ret_5d"].fillna(0) * st["ret_1m"].fillna(0)) < 0
        ).astype(float)
        score_st -= 0.15 * conflicting_5d_1m

    # 1m vs 3m: contradicción de plazo → -0.20
    if "ret_1m" in st.columns and "ret_3m" in st.columns:
        conflicting = (
            (st["ret_1m"].fillna(0) * st["ret_3m"].fillna(0)) < 0
        ).astype(float)
        score_st -= 0.20 * conflicting

    # Sentiment carry: sentimiento positivo del run anterior → continúa momentum
    if prev_sentiment:
        carry = pd.Series(
            [float(prev_sentiment.get(t, 0.0)) for t in st.index], index=st.index
        ).clip(-1.0, 1.0)
        score_st += 0.12 * carry
        nonzero = carry[carry != 0]
        if not nonzero.empty:
            logger.info(
                "Sentiment carry aplicado: %s",
                {t: f"{v:+.2f}" for t, v in nonzero.items()},
            )

    # Boost sectorial CP (más agresivo que LP)
    score_st *= pd.Series(
        [sector_boost.get(t, 1.0) for t in st.index], index=st.index
    )

    # Bonus intraday 15 min: momentum y VWAP del día actual
    intraday_series = pd.Series(
        [intraday_scores.get(t, 0.0) for t in st.index], index=st.index
    )
    score_st += 0.20 * intraday_series   # score ya está en [-1, +1]

    # Insider buy bonus CP: más impacto que LP (señal de momentum inminente)
    if insider_signal:
        insider_cp = pd.Series(
            [insider_signal.get(t, 0.0) for t in st.index], index=st.index
        ).clip(lower=0)
        score_st += 0.25 * insider_cp
        nonzero_ins = insider_cp[insider_cp > 0]
        if not nonzero_ins.empty:
            logger.info("Insider buy bonus CP: %s", {t: f"+{v:.2f}" for t, v in nonzero_ins.items()})

    # Fear & Greed CP: el F&G extremo afecta la urgencia de las señales
    if fear_greed is not None:
        if fear_greed <= 20:       # pánico extremo → señal contrarian muy fuerte
            score_st *= 1.15
            logger.info("F&G %d (extreme fear) → CP scores ×1.15", fear_greed)
        elif fear_greed <= 30:
            score_st *= 1.08
        elif fear_greed >= 85:     # euforia extrema → señal muy débil
            score_st *= 0.85
            logger.info("F&G %d (extreme greed) → CP scores ×0.85", fear_greed)
        elif fear_greed >= 75:
            score_st *= 0.92

    st["score_st"]      = score_st
    st["intraday_score"] = intraday_series

    # Quality bonus sobre top-15 CP (analyst rating + crecimiento revenue)
    # Solo top-15 para no hacer 49 llamadas a fundamentals
    top_cp_tickers = st.nlargest(15, "score_st").index.tolist()
    quality_cp = _get_quality_bonus(top_cp_tickers)
    quality_cp_series = pd.Series(
        [quality_cp.get(t, 0.0) for t in st.index], index=st.index
    )
    # En CP el quality bonus es 50% menos impactante (prioridad: momentum)
    st["score_st"] += 0.5 * quality_cp_series

    # ── Earnings guard ─────────────────────────────────────────────────────────
    # Penalizar posiciones con earnings inminentes para evitar riesgo binario.
    if earnings_soon:
        logger.warning("Earnings guard activo: %s", sorted(earnings_soon))
        lp["earnings_soon"] = lp.index.isin(earnings_soon).astype(int)
        st["earnings_soon"] = st.index.isin(earnings_soon).astype(int)
        lp.loc[lp.index.isin(earnings_soon), "score_lp"] -= 0.5
        st.loc[st.index.isin(earnings_soon), "score_st"] -= 0.8
    else:
        lp["earnings_soon"] = 0
        st["earnings_soon"] = 0

    st = st.sort_values("score_st", ascending=False)
    lp = lp.sort_values("score_lp", ascending=False)

    return {"long_term": lp, "short_term": st}
