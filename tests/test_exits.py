"""Tests de protección de ganadores + reconciliación + headroom (iter18-19)."""
from trader_agent.portfolio import diff_against_current, check_capital_headroom, TradeIntent


class _Pos:
    def __init__(self, ticker, mv, avg, qty):
        self.ticker = ticker
        self.market_value = mv
        self.avg_price = avg
        self.qty = qty
        self.asset_class = "equity"


def test_winner_protection_keeps_profitable_rotated_out():
    # Ambos salieron del target; el ganador no se vende, el perdedor sí.
    positions = [_Pos("WIN", 110.0, 10.0, 10.0), _Pos("LOSE", 95.0, 10.0, 10.0)]
    intents = diff_against_current({}, positions, threshold=25.0)
    sells = {i.ticker for i in intents if i.side == "SELL"}
    assert "WIN" not in sells
    assert "LOSE" in sells


def test_winner_in_target_still_trims_overexposure():
    # Ganador que sigue en target pero sobre-expuesto → se recorta el exceso.
    positions = [_Pos("WIN", 500.0, 10.0, 50.0)]  # +0% pero mv 500
    target = {"WIN": {"notional": 100.0, "horizon": "CP"}}
    intents = diff_against_current(target, positions, threshold=25.0)
    sells = [i for i in intents if i.side == "SELL" and i.ticker == "WIN"]
    assert sells and abs(sells[0].notional - 400.0) < 1e-6  # vende el exceso 500-100


def test_headroom_no_double_subtraction(tmp_path):
    # iter19: equity 1722, bp 970, invertido 751 → headroom ~970 (no 219).
    pos = [_Pos("HELD", 751.0, 751.0, 1.0)]
    buys = [TradeIntent(ticker="NEW", side="BUY", notional=900.0, horizon="CP",
                        stop_loss=None, take_profit=None)]
    out = check_capital_headroom(1722.0, pos, list(buys), buying_power=970.0)
    deployed = sum(i.notional for i in out if i.side == "BUY")
    assert deployed >= 850  # despliega el cash real, no $219


def test_headroom_no_margin_abuse():
    # equity 1722, invertido 1600, bp alto → solo ~122 libres (sin margen)
    pos = [_Pos("HELD", 1600.0, 1600.0, 1.0)]
    buys = [TradeIntent(ticker="NEW", side="BUY", notional=900.0, horizon="CP",
                        stop_loss=None, take_profit=None)]
    out = check_capital_headroom(1722.0, pos, list(buys), buying_power=5000.0)
    deployed = sum(i.notional for i in out if i.side == "BUY")
    assert deployed <= 200  # no usa margen aunque bp lo permita


# ── iter31: gate de rotación de entradas mensual ───────────────────────────

def _full_book_target():
    held = [_Pos(t, 300.0, 30.0, 10.0) for t in ("B", "C", "D", "E", "F")]
    target = {
        "C": {"notional": 300.0, "horizon": "CP"},
        "D": {"notional": 300.0, "horizon": "CP"},
        "E": {"notional": 300.0, "horizon": "CP"},
        "F": {"notional": 300.0, "horizon": "CP"},
        "NEW": {"notional": 300.0, "horizon": "CP", "stop_loss": 25.0, "take_profit": 40.0},
    }
    return held, target


def test_entry_gate_open_allows_new_name():
    held, target = _full_book_target()
    intents = diff_against_current(target, held, threshold=25.0, entry_open=True)
    buys = {i.ticker for i in intents if i.side == "BUY"}
    assert "NEW" in buys  # ventana abierta → nombre nuevo entra


def test_entry_gate_closed_blocks_new_name_when_book_full():
    held, target = _full_book_target()
    intents = diff_against_current(target, held, threshold=25.0, entry_open=False)
    buys = {i.ticker for i in intents if i.side == "BUY"}
    assert "NEW" not in buys  # ventana cerrada + libro lleno → sin nombres nuevos


def test_entry_gate_closed_allows_backfill_when_underdeployed():
    # Solo holdeo 2 de 5 slots → backfill permitido aunque la ventana esté cerrada.
    held = [_Pos(t, 300.0, 30.0, 10.0) for t in ("B", "C")]
    _, target = _full_book_target()
    intents = diff_against_current(target, held, threshold=25.0, entry_open=False)
    buys = {i.ticker for i in intents if i.side == "BUY"}
    assert "NEW" in buys and "D" in buys  # anti cash-drag: llena el libro


def test_entry_window_open_fail_safe(tmp_path, monkeypatch):
    import trader_agent.portfolio as pf
    from datetime import date, timedelta
    # Sin archivo → True (fail-safe, no bloquea)
    monkeypatch.setattr(pf, "_ENTRY_GATE_PATH", tmp_path / "entry_gate.json")
    assert pf.entry_window_open() is True
    # Recién rotado hoy → ventana cerrada
    pf.record_entry_rotation()
    assert pf.entry_window_open(min_days=21) is False
    # Rotación vieja (>21d) → ventana abierta
    old = (date.today() - timedelta(days=30)).isoformat()
    (tmp_path / "entry_gate.json").write_text('{"last_entry_date": "%s"}' % old, encoding="utf-8")
    assert pf.entry_window_open(min_days=21) is True


def test_reconcile_hold_days_never_negative(tmp_path, monkeypatch):
    import alpha_agent.analytics.trade_db as tdb
    monkeypatch.setattr(tdb, "_DB_PATH", tmp_path / "t.db")
    tdb.init_db()
    # BUY hoy, SELL "anterior" no debe matchear (ts <= sell_ts) → sin holds negativos
    tdb.log_trade(ticker="ZZ", side="BUY", qty=1, price=100, sleeve="CP")
    # un SELL con ts viejo: no debe cerrar el BUY nuevo
    import sqlite3
    with sqlite3.connect(str(tmp_path / "t.db")) as c:
        c.execute("INSERT INTO trades (ts, date, ticker, side, qty, price) "
                  "VALUES ('2020-01-01T00:00:00','2020-01-01','ZZ','SELL',1,90)")
    tdb.reconcile_buy_sell_pairs()
    for tr in tdb.get_trades(ticker="ZZ"):
        hd = tr.get("hold_days")
        assert hd is None or hd >= 0, f"hold_days negativo: {hd}"
