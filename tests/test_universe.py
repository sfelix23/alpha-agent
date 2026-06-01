"""Tests del universo focalizado + rotación automática (iter17)."""
import json

import alpha_agent.config as cfg
from alpha_agent.config import ACTIVOS, UNIVERSE, CP_UNIVERSE, get_effective_cp_universe
import alpha_agent.discovery.universe_scanner as us


# ── Parte A: recorte de ilíquidos ──────────────────────────────────────────
def test_universe_trim_removes_illiquid_adrs():
    illiquid = {"LOMA", "TGS", "EDN", "PAM", "IRS", "DESP", "TGNO4.BA"}
    assert not (illiquid & set(ACTIVOS.values())), "quedaron ADRs ilíquidos en ACTIVOS"
    assert "TGS" not in UNIVERSE
    # los líquidos con edge se mantienen
    for keep in ("GGAL", "BMA", "MELI", "VIST", "YPF"):
        assert keep in ACTIVOS.values()


# ── Parte C.1: universo efectivo con overrides ─────────────────────────────
def test_effective_universe_no_overrides(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "__file__", str(tmp_path / "config.py"))
    # sin archivo de overrides → estático
    assert get_effective_cp_universe() == CP_UNIVERSE


def test_effective_universe_with_overrides(tmp_path, monkeypatch):
    sig = tmp_path / "signals"
    sig.mkdir(parents=True, exist_ok=True)
    (sig / "cp_universe_overrides.json").write_text(json.dumps(
        {"added": ["ZZZZ"], "removed": ["NVDA"], "vetoed": ["TSLA"]}
    ))
    monkeypatch.setattr(cfg, "__file__", str(tmp_path / "pkg" / "config.py"))
    eff = get_effective_cp_universe()
    assert "ZZZZ" in eff and "NVDA" not in eff and "TSLA" not in eff


# ── Parte C.2: rotación con guardarraíles ──────────────────────────────────
class _FakeBroker:
    def __init__(self, held=None):
        self._held = held or []
    def get_positions(self):
        return [type("P", (), {"ticker": t})() for t in self._held]


def _stub_scores(monkeypatch):
    def fake(t):
        scores = {"STRONG": 3.15, "OXY": 0.21}
        s = scores.get(t, 1.03)
        return {"ticker": t, "sharpe_3m": s, "mom_1m_pct": 1.0, "price": 50.0,
                "avg_dollar_vol": 5e8, "combined_score": s}
    monkeypatch.setattr(us, "_quick_score", fake)


def test_rotation_swaps_strong_candidate(tmp_path, monkeypatch):
    monkeypatch.setattr(us, "_OVERRIDES_PATH", tmp_path / "ov.json")
    _stub_scores(monkeypatch)
    disc = {"candidates": [{"ticker": "STRONG"}], "repeated_alerts": ["STRONG"]}
    msg = us._rotate_universe(disc, broker=_FakeBroker())
    assert msg and "STRONG" in msg
    ov = json.loads((tmp_path / "ov.json").read_text())
    assert "STRONG" in ov["added"]
    assert ov["removed"]  # sacó al más flojo (OXY, no protegido)


def test_rotation_max_one_per_week(tmp_path, monkeypatch):
    monkeypatch.setattr(us, "_OVERRIDES_PATH", tmp_path / "ov.json")
    _stub_scores(monkeypatch)
    disc = {"candidates": [{"ticker": "STRONG"}], "repeated_alerts": ["STRONG"]}
    assert us._rotate_universe(disc, broker=_FakeBroker())      # 1er swap OK
    assert not us._rotate_universe(disc, broker=_FakeBroker())  # 2do bloqueado misma semana


def test_rotation_respects_veto(tmp_path, monkeypatch):
    ov = tmp_path / "ov.json"
    ov.write_text(json.dumps({"added": [], "removed": [], "vetoed": ["STRONG"],
                              "history": [], "last_swap_week": None}))
    monkeypatch.setattr(us, "_OVERRIDES_PATH", ov)
    _stub_scores(monkeypatch)
    disc = {"candidates": [{"ticker": "STRONG"}], "repeated_alerts": ["STRONG"]}
    assert not us._rotate_universe(disc, broker=_FakeBroker())  # vetado → no entra


def test_rotation_skips_when_no_repeated(tmp_path, monkeypatch):
    monkeypatch.setattr(us, "_OVERRIDES_PATH", tmp_path / "ov.json")
    _stub_scores(monkeypatch)
    disc = {"candidates": [{"ticker": "STRONG"}], "repeated_alerts": []}
    assert not us._rotate_universe(disc, broker=_FakeBroker())  # no repetido → sin swap


# ── iter53: radar ampliado (S&P 500 completo + ETFs, descarga en chunks) ─────
def test_scan_opportunities_includes_full_universe_and_etfs(tmp_path, monkeypatch):
    """El radar escanea S&P 500 completo + ETFs, no solo los primeros 160."""
    import numpy as np
    import pandas as pd
    import alpha_agent.data.market_data as md

    # S&P 500 "completo" simulado: nombres de TODO el abecedario (incl. los que
    # el cap de 160 alfabético dejaba afuera: NVDA, TSLA, WMT).
    sp500 = [f"AA{i}" for i in range(200)] + ["NVDA", "TSLA", "WMT"]
    monkeypatch.setattr(us, "_get_sp500_tickers", lambda: sp500)

    # iter54: índices extendidos mockeados (sin red). mid/small caps de prueba.
    def _fake_index(url, label, min_count=50):
        if "400" in url:
            return ["MIDA", "MIDB"]
        if "600" in url:
            return ["SMLA", "SMLB"]
        return ["NDXONLY"]  # nasdaq-100 con un nombre no-S&P
    monkeypatch.setattr(us, "_get_index_tickers", _fake_index)

    idx = pd.date_range("2024-01-01", periods=260, freq="B")
    requested_chunks = []

    def _fake_dl(tickers, label):
        requested_chunks.append(list(tickers))
        # serie con tendencia alcista suave para que pase los filtros de len>=60
        data = {t: np.linspace(100, 130, len(idx)) for t in tickers}
        return pd.DataFrame(data, index=idx)

    monkeypatch.setattr(md, "_download_close", _fake_dl)
    monkeypatch.setattr(us, "OPPORTUNITIES_PATH", tmp_path / "opp.json")

    r = us.scan_opportunities()  # None = todo
    all_requested = {t for chunk in requested_chunks for t in chunk}

    # nombres del final del abecedario AHORA se escanean (antes excluidos por [:160])
    assert {"NVDA", "TSLA", "WMT"} <= all_requested
    # los ETFs entran al universo del radar
    assert {"SMH", "XLK", "ITA"} <= all_requested
    # iter54: mid/small caps + nasdaq-only también se escanean
    assert {"MIDA", "SMLA", "NDXONLY"} <= all_requested
    # se descargó en más de un chunk (>100 tickers → chunking activo)
    assert len(requested_chunks) >= 2
    # los ETFs se etiquetan como sector "ETF"
    etf_opps = [o for o in r["opportunities"] if o.get("is_etf")]
    assert all(o["sector"] == "ETF" for o in etf_opps)
    # iter54: tiers contabilizados (by_tier cuenta sobre TODOS los opps, no el top-25)
    assert "by_tier" in r
    assert r["by_tier"].get("mid", 0) >= 2     # MIDA, MIDB
    assert r["by_tier"].get("small", 0) >= 2   # SMLA, SMLB
    assert r["by_tier"].get("etf", 0) >= 1
