"""Tests del core de decisión: allocation_agent + scoring (iter32).

Cubren invariantes que NO deben romperse sin re-backtestear:
  - allocation: cp_pct acotado, n_cp en rango, defensivo en BEAR/VIX alto,
    agresivo en BULL, sleeves suman <= 1.
  - scoring: cp_vol_penalty=0.0 es un no-op exacto (no cambia el ranking);
    backtest_mode no filtra al universo CP curado.
"""
import pandas as pd
import pytest

from alpha_agent.analytics.allocation_agent import decide_allocation
from alpha_agent.config import PARAMS


# ── allocation_agent ────────────────────────────────────────────────────────

@pytest.mark.parametrize("regime,vix", [
    ("BULL", 13.0), ("BULL", 22.0), ("NEUTRAL", 15.0),
    ("LATERAL", 18.0), ("BEAR", 35.0), ("BEAR", 20.0),
])
def test_allocation_invariants(regime, vix):
    d = decide_allocation(regime=regime, vix=vix)
    assert 0.0 <= d.cp_pct <= 0.95, f"cp_pct fuera de rango: {d.cp_pct}"
    assert 0.0 <= d.opt_pct <= 0.30, f"opt_pct fuera de rango: {d.opt_pct}"
    assert d.lp_pct == 0.0, "LP debe estar en 0 (desactivado < $5k)"
    assert 1 <= d.n_cp_positions <= 8, f"n_cp fuera de rango: {d.n_cp_positions}"
    # Sleeves no pueden sumar > 100%
    assert d.lp_pct + d.cp_pct + d.opt_pct <= 1.0 + 1e-9
    assert d.level in (1, 2, 3)


def test_allocation_defensive_in_bear():
    bear = decide_allocation(regime="BEAR", vix=35.0)
    bull = decide_allocation(regime="BULL", vix=13.0)
    # En BEAR/VIX alto la exposición CP debe ser MENOR que en BULL tranquilo.
    assert bear.cp_pct < bull.cp_pct
    assert bear.level == 3


def test_allocation_diversified_sizing():
    # iter29: el sizing debe diversificar (5-6 posiciones), no concentrar en 2.
    d = decide_allocation(regime="BULL", vix=13.0)
    assert d.n_cp_positions >= 5, "iter29: BULL debe diversificar a >=5 posiciones"


# ── scoring: cp_vol_penalty wiring ──────────────────────────────────────────

def _toy_frames():
    """capm + technical mínimos para build_scores sobre 4 tickers."""
    idx = ["AAA", "BBB", "CCC", "DDD"]
    capm = pd.DataFrame({
        "beta": [1.0, 1.2, 0.8, 1.1],
        "sharpe": [1.5, 1.2, 0.9, 1.0],
        "alpha_jensen": [0.05, 0.03, 0.01, 0.02],
        "information_ratio": [0.5, 0.4, 0.2, 0.3],
        "mu_anual": [0.3, 0.25, 0.15, 0.2],
        "sigma_anual": [0.20, 0.60, 0.25, 0.45],  # BBB y DDD más volátiles
        "expected_return_capm": [0.1, 0.1, 0.1, 0.1],
    }, index=idx)
    technical = pd.DataFrame({
        "ret_1m": [0.08, 0.12, 0.04, 0.10],
        "ret_3m": [0.15, 0.20, 0.08, 0.18],
        "ret_6m": [0.30, 0.40, 0.15, 0.35],
        "ret_5d": [0.02, 0.03, 0.01, 0.025],
        "rsi": [55, 60, 48, 58],
        "dist_52w_high": [-0.05, -0.02, -0.15, -0.03],
    }, index=idx)
    return capm, technical


def test_cp_vol_penalty_zero_is_noop(monkeypatch):
    from alpha_agent.analytics import scoring
    capm, tech = _toy_frames()

    object.__setattr__(PARAMS, "cp_vol_penalty", 0.0)
    s0 = scoring.build_scores(capm, tech, regime="BULL", backtest_mode=True)["short_term"]
    base = s0["score_st"].copy()

    object.__setattr__(PARAMS, "cp_vol_penalty", 0.30)
    s1 = scoring.build_scores(capm, tech, regime="BULL", backtest_mode=True)["short_term"]
    pen = s1["score_st"].copy()

    # La palanca debe ACTUAR: los nombres de ALTA vol (BBB σ=0.60, DDD σ=0.45)
    # bajan su score, los de BAJA vol (AAA σ=0.20, CCC σ=0.25) suben — relativo a pen=0.
    assert pen["BBB"] < base["BBB"], "alta vol (BBB) debe bajar con penalty>0"
    assert pen["DDD"] < base["DDD"], "alta vol (DDD) debe bajar con penalty>0"
    assert pen["AAA"] > base["AAA"], "baja vol (AAA) debe subir con penalty>0"

    # Restaurar el default de producción (0.0) y verificar que es un NO-OP exacto.
    object.__setattr__(PARAMS, "cp_vol_penalty", 0.0)
    s2 = scoring.build_scores(capm, tech, regime="BULL", backtest_mode=True)["short_term"]
    assert (s2["score_st"] - base).abs().max() < 1e-9, "0.0 debe reproducir el score sin penalty"


# ── signals: cap_floor_weights (iter35) ─────────────────────────────────────

def test_cap_floor_weights_basic():
    """5 pesos donde 1 absorbe 50% → cap 0.35 + floor 0.12, suma 1.0."""
    from alpha_agent.reporting.signals import cap_floor_weights
    # ALTA gana 50%, otras 4 a 12.5% c/u
    ws = cap_floor_weights([0.50, 0.125, 0.125, 0.125, 0.125], cap=0.35, floor=0.12)
    assert abs(sum(ws) - 1.0) < 0.01, f"suma debe ser ~1: {sum(ws)}"
    assert max(ws) <= 0.35 + 1e-6, f"cap violado: max={max(ws)}"
    assert min(ws) >= 0.12 - 1e-6, f"floor violado: min={min(ws)}"


def test_cap_floor_redistributes_excess():
    """Excess de un cap debe ir a los no-capeados, no quedarse en el aire."""
    from alpha_agent.reporting.signals import cap_floor_weights
    ws = cap_floor_weights([0.80, 0.05, 0.05, 0.05, 0.05], cap=0.35, floor=0.12)
    assert abs(sum(ws) - 1.0) < 0.01
    assert ws[0] <= 0.35 + 1e-6
    # Los otros 4 subieron del floor (0.12) por la redistribución
    assert all(w >= 0.12 - 1e-6 for w in ws[1:])


def test_cap_floor_no_op_when_balanced():
    """Si los pesos ya están en rango [floor, cap], la función no los altera."""
    from alpha_agent.reporting.signals import cap_floor_weights
    initial = [0.25, 0.20, 0.20, 0.20, 0.15]
    ws = cap_floor_weights(initial, cap=0.35, floor=0.12)
    for a, b in zip(initial, ws):
        assert abs(a - b) < 0.01, f"se modificó un peso ya balanceado: {a} -> {b}"


def test_cap_floor_degraded_when_floor_too_high():
    """Si floor * n > 1, retorna equal-weight (no se puede cumplir el floor)."""
    from alpha_agent.reporting.signals import cap_floor_weights
    ws = cap_floor_weights([0.5, 0.3, 0.2], cap=0.35, floor=0.50)
    assert all(abs(w - 1/3) < 1e-6 for w in ws), f"esperaba equal-weight: {ws}"


# ── run_monitor: backstop de pérdida por trade (iter33) ─────────────────────

def test_max_loss_backstop():
    from run_monitor import max_loss_breached
    cap = 0.08  # -8%
    # Cola catastrófica → corta
    assert max_loss_breached(-9.6, 100.0, cap) is True
    assert max_loss_breached(-8.0, 100.0, cap) is True
    # Perdedores normales → NO toca (el peor "normal" observado fue -4.3%)
    assert max_loss_breached(-4.3, 100.0, cap) is False
    assert max_loss_breached(-1.0, 100.0, cap) is False
    # Ganadores → nunca
    assert max_loss_breached(6.5, 100.0, cap) is False
    # Desactivado (0.0) → nunca corta
    assert max_loss_breached(-50.0, 100.0, 0.0) is False
    # Sin avg_entry válido → no corta (evita falsos positivos en dust/datos malos)
    assert max_loss_breached(-20.0, 0.0, cap) is False
