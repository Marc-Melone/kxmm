import json
from pathlib import Path

from kxmm import db

FIX = Path(__file__).parent / "fixtures"


def fixture(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def gaps(con):
    return [tuple(r) for r in con.execute(
        "SELECT ticker, start_ts, end_ts FROM gaps ORDER BY ticker, start_ts")]


def test_store_book_dedupes_unchanged(tmp_path):
    con = db.connect(tmp_path / "out" / "t.db")
    raw = fixture("orderbook.json")["orderbook_fp"]
    assert db.store_book(con, "T", 1.0, raw) is True
    assert db.store_book(con, "T", 2.0, raw) is False
    assert con.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 1


def test_store_trades_dedupes_by_trade_id(tmp_path):
    con = db.connect(tmp_path / "t.db")
    rows = fixture("trades.json")["trades"]
    assert db.store_trades(con, rows) == len(rows)
    assert db.store_trades(con, rows) == 0
    r = con.execute("SELECT * FROM trades ORDER BY ts DESC LIMIT 1").fetchone()
    assert r["yes_price"] == 0.99 and r["count"] == 15.0 and r["taker_side"] == "yes"


def test_mark_poll_gap_only_over_threshold(tmp_path):
    con = db.connect(tmp_path / "t.db")
    for ts in (0, 200, 600):
        db.mark_poll(con, "T", ts, 300)
    assert gaps(con) == [("T", 200, 600)]


def test_restart_gaps_cover_downtime(tmp_path):
    con = db.connect(tmp_path / "t.db")
    db.mark_poll(con, "T", 0, 300)
    db.heartbeat(con, 0, 300)
    assert db.record_restart_gaps(con, 1000, 300) == 2
    assert gaps(con) == [("*", 0, 1000), ("T", 0, 1000)]
    assert db.last_heartbeat(con) == 1000


def test_upsert_markets_derives_series(tmp_path):
    con = db.connect(tmp_path / "t.db")
    m = fixture("market.json")["market"]
    db.upsert_markets(con, [m], {"KXSTREAMARRAY": fixture("series.json")["series"]}, 5.0)
    r = con.execute("SELECT * FROM markets").fetchone()
    assert r["series_ticker"] == "KXSTREAMARRAY"
    assert r["category"] == "Financials" and r["fee_type"] == "quadratic"
