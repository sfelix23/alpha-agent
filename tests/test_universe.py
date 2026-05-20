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
