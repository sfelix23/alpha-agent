"""Tests de protección de ganadores + reconciliación de holds (iter18)."""
from trader_agent.portfolio import diff_against_current


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
