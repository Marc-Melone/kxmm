from kxmm import tradedb


def fill(fid, ts="2026-11-17T10:00:00Z"):
    return {"fill_id": fid, "created_time": ts, "ticker": "K", "book_side": "bid",
            "yes_price_dollars": "0.4900", "count_fp": "1.00", "fee_cost": "0.0000",
            "order_id": "o1"}


def test_add_fills_returns_only_new(tmp_path):
    con = tradedb.connect(tmp_path / "trade" / "trade.db")
    assert [f["fill_id"] for f in tradedb.add_fills(con, [fill("a"), fill("b")])] == ["a", "b"]
    assert [f["fill_id"] for f in tradedb.add_fills(con, [fill("b"), fill("c")])] == ["c"]
    r = con.execute("SELECT * FROM fills WHERE fill_id='a'").fetchone()
    assert (r["book_side"], r["yes_price"], r["count"], r["fee"]) == ("bid", 0.49, 1.0, 0.0)


def test_events_and_drills_round_trip(tmp_path):
    con = tradedb.connect(tmp_path / "trade.db")
    tradedb.event(con, 1.0, "halt", "reconcile: K local 1 vs Kalshi 0")
    tradedb.drill_result(con, 2.0, "kill", True, "orders empty in 4 s")
    tradedb.heartbeat(con, 3.0)
    assert con.execute("SELECT kind, detail FROM events").fetchone()[:] == (
        "halt", "reconcile: K local 1 vs Kalshi 0")
    assert con.execute("SELECT name, passed FROM drills").fetchone()[:] == ("kill", 1)
    assert tradedb.kv_get(con, "heartbeat") == "3.0"


def test_heartbeat_alone_is_not_uptime(tmp_path):
    con = tradedb.connect(tmp_path / "trade.db")
    tradedb.heartbeat(con, 60.0)                 # a request attempt, even a failed one
    assert con.execute("SELECT COUNT(*) FROM uptime").fetchone()[0] == 0
    tradedb.mark_up(con, 61.0)                   # a completed trading cycle
    assert con.execute("SELECT COUNT(*) FROM uptime").fetchone()[0] == 1
