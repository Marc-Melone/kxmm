import json
import sqlite3
from pathlib import Path

import pytest

from kxmm import db
from kxmm.replay import (Program, events, market_meta, open_readonly, program_at,
                         MidSeries)
from tests.helpers import CENT, iso, raw_book

FIX = Path(__file__).parent / "fixtures"
T = 1_800_000_000


def trade(i, ts, yes_price, count, side):
    return {"trade_id": f"t{i}", "ticker": "K", "created_time": iso(ts),
            "yes_price_dollars": f"{yes_price:.4f}", "count_fp": f"{count:.2f}",
            "taker_side": side}


def make_db(tmp_path):
    path = tmp_path / "rec.db"
    con = db.connect(path)
    db.upsert_markets(con, [{"ticker": "K", "event_ticker": "EK-1", "status": "active",
                             "close_time": iso(T + 30 * 86400),
                             "occurrence_datetime": iso(T + 20 * 86400),
                             "price_ranges": CENT}],
                      {"EK": {"fee_type": "quadratic", "category": "X"}}, T)
    db.store_book(con, "K", T + 0, raw_book([(0.48, 100)], [(0.50, 100)]))
    db.store_book(con, "K", T + 100, raw_book([(0.49, 100)], [(0.50, 100)]))
    db.store_book(con, "K", T + 400, raw_book([(0.47, 100)], [(0.50, 100)]))
    db.store_trades(con, [trade(1, T + 50, 0.48, 5, "no"), trade(2, T + 150, 0.50, 3, "yes")])
    db.add_gap(con, "K", T + 200, T + 300)
    con.close()
    return path


def test_events_in_time_order_with_gaps(tmp_path):
    con = open_readonly(make_db(tmp_path))
    kinds = [e.kind for e in events(con, "K", T - 1, T + 1000)]
    assert kinds == ["book", "trade", "book", "trade", "gap_start", "gap_end", "book"]
    ev = events(con, "K", T - 1, T + 1000)
    assert ev[1].trade.taker_side == "no" and ev[1].trade.count == 5.0
    assert ev[0].book.best_yes_bid == pytest.approx(0.48)


def test_mid_series_respects_gaps(tmp_path):
    mids = MidSeries(open_readonly(make_db(tmp_path)), "K")
    assert mids.at(T + 120) == pytest.approx(0.495)
    assert mids.at(T + 250) is None
    assert mids.at(T - 1) is None


def test_program_at_picks_largest_active():
    progs = [Program(start=0, end=100, daily_pool=10, target=1000, discount=0.5),
             Program(start=50, end=200, daily_pool=40, target=1000, discount=0.5)]
    assert program_at(progs, 60).daily_pool == 40
    assert program_at(progs, 20).daily_pool == 10
    assert program_at(progs, 500) is None


def test_market_meta_reads_occurrence_and_fee(tmp_path):
    con = db.connect(tmp_path / "rec.db")
    m = json.loads((FIX / "market.json").read_text(encoding="utf-8"))["market"]
    db.upsert_markets(con, [m], {"KXSTREAMARRAY": {"fee_type": "quadratic_with_maker_fees"}}, T)
    meta = market_meta(open_readonly(tmp_path / "rec.db"), m["ticker"])
    assert meta.maker_fee is True
    assert meta.occurrence_ts == db.iso_ts(m["occurrence_datetime"])
    assert meta.close_ts == db.iso_ts(m["close_time"])
    assert meta.price_ranges == m["price_ranges"]


def test_open_readonly_refuses_writes(tmp_path):
    con = open_readonly(make_db(tmp_path))
    with pytest.raises(sqlite3.OperationalError):
        con.execute("INSERT INTO kv(k, v) VALUES('x', 'y')")


def test_trade_and_loop_gaps_also_pause_replay(tmp_path):
    path = make_db(tmp_path)
    con = db.connect(path)
    db.add_gap(con, "trades:K", T + 500, T + 600)    # trades unobserved: no fills knowable
    db.add_gap(con, "*", T + 700, T + 800)           # whole loop down
    con.close()
    kinds = [(e.kind, e.ts - T) for e in events(open_readonly(path), "K", T + 450, T + 900)]
    assert kinds == [("gap_start", 500), ("gap_end", 600), ("gap_start", 700), ("gap_end", 800)]


def test_nested_gaps_merge_into_one(tmp_path):
    # the recorder's real pattern: loop gaps ('*') nested inside a market's own gap
    path = make_db(tmp_path)
    con = db.connect(path)
    db.add_gap(con, "K", T + 1000, T + 1400)
    db.add_gap(con, "*", T + 1010, T + 1300)
    db.add_gap(con, "*", T + 1320, T + 1350)
    db.add_gap(con, "trades:K", T + 1380, T + 1450)      # overlaps the end: extends it
    con.close()
    kinds = [(e.kind, e.ts - T) for e in events(open_readonly(path), "K", T + 900, T + 2000)]
    assert kinds == [("gap_start", 1000), ("gap_end", 1450)]


def test_replay_starting_inside_a_gap_starts_in_gap(tmp_path):
    kinds = [(e.kind, e.ts - T) for e in events(open_readonly(make_db(tmp_path)), "K",
                                                T + 250, T + 1000)]
    assert kinds[:2] == [("gap_start", 250), ("gap_end", 300)]


def test_mid_series_none_outside_polled_coverage(tmp_path):
    path = make_db(tmp_path)
    con = db.connect(path)
    db.mark_poll(con, "K", T + 0, 10**9)
    db.mark_poll(con, "K", T + 450, 10**9)                # last good poll
    con.close()
    mids = MidSeries(open_readonly(path), "K")
    assert mids.at(T + 420) == pytest.approx(0.485)      # book at 400: 0.47 / ask 0.50
    assert mids.at(T + 500) is None                       # nothing observed after 450
    assert mids.last_known(T + 500) == pytest.approx(0.485)
