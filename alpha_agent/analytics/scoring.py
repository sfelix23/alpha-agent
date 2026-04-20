"""
Scoring compuesto LP / CP + guards de diversificación.

Pipeline:
    1. Filtro de calidad LP (beta razonable + Sharpe mínimo).
    2. Ranking inicial por Sharpe.
    3. **Guard de correlación**: iterativamente agrega al shortlist el siguiente
       mejor activo que NO esté altamente correlacionado con los ya elegidos.
       Evita que la cartera termine con 4 commodities cuando el universo tiene 47.
    4. **Guard sectorial**: limita el peso por sector (MAX_SECTOR_WEIGHT_LP).
    5. Short-term scoring con técnicos (RSI, momentum, alpha).
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
    """
    Greedy filter: recorre candidatos ordenados por ranking y solo acepta
    aquellos que (a) no estén fuertemente correlacionados con los ya aceptados,
    y (b) no excedan el cupo de activos por sector.

    Args:
        candidates_sorted: tickers ordenados por score descendente.
        returns: DataFrame de retornos diarios (cols=ticker).
        max_pair_corr: correlación máxima ρ entre cualquier par aceptado.
        max_sector_count: máximo de activos aceptados por sector.

    Returns:
        Lista filtrada (mantiene orden del ranking).
    """
    accepted: list[str] = []
    sector_count: dict[str, int] = {}
    corr_matrix = returns.corr()

    for t in candidates_sorted:
        if t not in corr_matrix.columns:
            continue
        sector = SECTOR_MAP.get(t, "Other")
        # cupo sectorial
        if sector_count.get(sector, 0) >= max_sector_count:
            continue
        # correlación con los ya aceptados
        skip = False
        for acc in accepted:
            rho = corr_matrix.loc[t, acc]
            if pd.notna(rho) and abs(rho) > max_pair_corr:
                logger.info(
                    "  ⊘ %s descartado: ρ=%.2f con %s (sector %s)",
                    t, rho, acc, sector,
                )
                skip = True
                break
        if skip:
            continue
        accepted.append(t)
        sector_count[sector] = sector_count.get(sector, 0) + 1

    return accepted


def compute_sector_weights(weights: pd.Series) -> dict[str, float]:
    """Devuelve el peso agregado por sector de una serie de pesos."""
    out: dict[str, float] = {}
    for t, w in weights.items():
        sector = SECTOR_MAP.get(t, "Other")
        out[sector] = out.get(sector, 0.0) + float(w)
    return out


def build_scores(
    capm: pd.DataFrame,
    technical: pd.DataFrame,
    *,
    closes: pd.DataFrame | None = None,
) -> dict[str, pd.DataFrame]:
    """
    Devuelve {'long_term': df, 'short_term': df} con rankings.

    Si se pasa `closes`, aplica el guard de correlación y sector sobre los
    candidatos LP (shortlist más diversa).
    """
    df = capm.join(technical, how="inner")

    # ── LONG TERM ──────────────────────────────────────────────────────
    lp = df[
        (df["beta"].abs() <= PARAMS.max_beta_lp)
        & (df["sharpe"] >= PARAMS.min_sharpe_lp)
    ].copy()
    lp = lp.sort_values("sharpe", ascending=False)
    lp["score_lp"] = _zscore(lp["sharpe"]) + 0.5 * _zscore(lp["alpha_jensen"])

    # Guard de correlación y sector (si tenemos los precios)
    if closes is not None and len(lp) > 1:
        returns = np.log(closes / closes.shift(1)).dropna(how="all")
        returns = returns[[c for c in lp.index if c in returns.columns]]
        ranking = lp.index.tolist()
        # tamaño del shortlist: el doble del top_n para dejarle margen a Markowitz
        target = PARAMS.top_n_long_term * 2
        logger.info("Aplicando guard correlación/sector a %d candidatos LP…", len(ranking))
        kept = filter_by_correlation_and_sector(ranking, returns)[:target]
        logger.info("Shortlist LP post-guard: %s", kept)
        lp = lp.loc[kept]

    # ── SHORT TERM ─────────────────────────────────────────────────────
    st = df.copy()
    rsi_signal = (PARAMS.rsi_oversold - st["rsi"]).clip(lower=0)  # solo cuenta si está en sobreventa
    high_penalty = st["dist_52w_high"].clip(upper=0).abs()        # penaliza si está pegado al techo

    score_st = (
        0.40 * _zscore(st["ret_1m"])
        + 0.30 * _zscore(st["ret_3m"])
        + 0.20 * _zscore(rsi_signal)
        + 0.10 * _zscore(st["alpha_jensen"].clip(lower=0))
        - 0.10 * _zscore(-high_penalty)  # más penalización si está pegado al techo
    )
    st["score_st"] = score_st
    st = st.sort_values("score_st", ascending=False)

    return {"long_term": lp, "short_term": st}
