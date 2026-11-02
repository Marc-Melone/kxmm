import pytest

from kxmm import db
from kxmm.config import load_config
from kxmm.replay import open_readonly
from kxmm.sim import maker_fee, simulate_market
from tests.helpers import CENT, iso, raw_book

CFG = load_config()
T = 1_800_000_000
LAG = CFG["sim"]["book_lag_s"]          # the first book counts as seen at t = LAG
YES_OK = [(0.50, 60), (0.49, 60)]      # depth 120 >= target 100
NO_OK = [(0.49, 60), (0.48, 60)]       # YES ask 0.51 -> mid 0.505; we quote YES 0.50, NO 0.49


def build(tmp_path, books, trades=(), gaps=(), close_days=10, covered_until=None,
          loop_gaps=(), close_s=None, program_end_s=30 * 86400):
    """books: [(dt, yes_levels, no_levels)], trades: [(dt, yes_price, count, taker_side)].
    Like the recorder, an unchanged book is not stored again; covered_until is the
    last successful poll (poll_state.last_ok)."""
    path = tmp_path / "rec.db"
    con = db.connect(path)
    db.upsert_markets(con, [{"ticker": "K", "event_ticker": "EK-1", "status": "active",
                             "close_time": iso(T + (close_s if close_s is not None
                                                    else close_days * 86400)),
                             "price_ranges": CENT}], {"EK": {"fee_type": "quadratic"}}, T)
    start, end = T - 86400, T + program_end_s         # $86.40/day
    days = (end - start) / 86400
    db.upsert_programs(con, [{"id": "p", "market_ticker": "K", "start_date": iso(start),
                              "end_date": iso(end), "period_reward": 86.4 * days * 10_000,
                              "target_size_fp": "100.00", "discount_factor_bps": 5000}], T)
    for dt, y, n in books:
        db.store_book(con, "K", T + dt, raw_book(y, n))
    db.store_trades(con, [{"trade_id": f"t{i}", "ticker": "K", "created_time": iso(T + dt),
                           "yes_price_dollars": f"{p:.4f}", "count_fp": f"{c:.2f}",
                           "taker_side": side} for i, (dt, p, c, side) in enumerate(trades)])
    for a, b in gaps:
        db.add_gap(con, "K", T + a, T + b)
    for a, b in loop_gaps:
        db.add_gap(con, "*", T + a, T + b)
    if covered_until is not None:
        db.mark_poll(con, "K", T + books[0][0], 10**9)
        db.mark_poll(con, "K", T + covered_until, 10**9)
    con.close()
    return open_readonly(path)


def run(con, t1=4000):
    return simulate_market(con, "K", T - 1, T + t1, CFG)


def test_through_trade_creates_fill_with_markout(tmp_path):
    later_yes, later_no = [(0.51, 60), (0.50, 60)], [(0.47, 60), (0.46, 60)]   # mid 0.52
    con = build(tmp_path, books=[(0, YES_OK, NO_OK), (10, YES_OK, NO_OK), (500, later_yes, later_no)],
                trades=[(20, 0.49, 5, "no")])
    r = run(con)
    assert len(r.fills) == 1
    f = r.fills[0]
    assert (f.side, f.price, f.size) == ("yes", pytest.approx(0.50), pytest.approx(5))
    assert f.markouts[600] == pytest.approx((0.52 - 0.50) * 5)
    assert r.position == pytest.approx(5) and r.cash == pytest.approx(-2.5)


def test_rewards_accrue_only_with_both_sides_meeting_target(tmp_path):
    # one stored book (unchanged after), polled until t+3600
    ok = run(build(tmp_path, books=[(0, YES_OK, NO_OK)], covered_until=3600))
    # our 10 of 100 qualifying score on each side -> 0.1 share; $86.40/day = $0.001/s
    assert sum(ok.reward_by_day.values()) == pytest.approx(0.001 * 0.1 * (3600 - LAG))
    (tmp_path / "thin").mkdir()
    thin_no = [(0.49, 30)]
    thin = run(build(tmp_path / "thin", books=[(0, YES_OK, thin_no)], covered_until=3600))
    assert sum(thin.reward_by_day.values()) == 0


def test_gap_cancels_orders_and_stops_rewards(tmp_path):
    # the book is unchanged across the gap, so nothing new was stored after it:
    # replay resumes at the gap's end with the last known book
    con = build(tmp_path, books=[(0, YES_OK, NO_OK)], trades=[(150, 0.40, 50, "no")],
                gaps=[(100, 200)], covered_until=300)
    r = run(con)
    assert r.fills == []                                   # the trade fell inside the gap
    assert sum(r.quoted_s_by_day.values()) == pytest.approx((100 - LAG) + (300 - 200))


def test_blackout_stops_quoting(tmp_path):
    con = build(tmp_path, books=[(0, YES_OK, NO_OK)], trades=[(20, 0.40, 50, "no")],
                close_days=2, covered_until=3600)
    r = run(con)
    assert r.fills == [] and sum(r.reward_by_day.values()) == 0


def test_inventory_cap_pulls_adding_side(tmp_path):
    con = build(tmp_path, books=[(0, YES_OK, NO_OK), (10, YES_OK, NO_OK)],
                trades=[(20 + 10 * i, 0.49, 10, "no") for i in range(4)])
    r = run(con)
    assert r.position * 0.505 <= 15 + 1e-9                 # $15 worst-case loss cap
    assert len(r.fills) == 3


def test_maker_fee_rounds_up_to_the_cent():
    assert maker_fee(10, 0.50) == pytest.approx(0.05)      # 0.04375 -> 5 cents
    assert maker_fee(1, 0.50) == pytest.approx(0.01)       # 0.0044 -> 1 cent


def test_nested_gap_never_quotes_inside_outer_gap(tmp_path):
    con = build(tmp_path, books=[(0, YES_OK, NO_OK)], trades=[(350, 0.40, 50, "no")],
                gaps=[(100, 400)], loop_gaps=[(110, 300)], covered_until=600)
    r = run(con)
    assert r.fills == []                                   # 350 is still inside [100, 400]
    assert sum(r.quoted_s_by_day.values()) == pytest.approx((100 - LAG) + (600 - 400))


def test_book_that_already_shows_a_trade_does_not_double_count_queue(tmp_path):
    # we join behind 60 at 0.50; the book at t=100 already shows the 40-lot trade
    # stamped t=101 by the exchange (local clock behind): the queue is used once
    after = [(0.50, 20), (0.49, 60)]
    con = build(tmp_path, books=[(0, YES_OK, NO_OK), (100, after, NO_OK)],
                trades=[(101, 0.50, 40, "no"), (200, 0.50, 30, "no")], covered_until=300)
    r = run(con)
    # the 40-lot at t=101 only eats the queue (60 -> 20); the 30-lot at t=200 fills our 10
    assert [(f.ts - T, round(f.size, 6)) for f in r.fills] == [(200, 10)]


def test_one_sweep_fills_an_order_once(tmp_path):
    # Kalshi prints one taker sweep as several trades with the same timestamp
    con = build(tmp_path, books=[(0, YES_OK, NO_OK)],
                trades=[(20, 0.49, 10, "no"), (20, 0.48, 10, "no"), (20, 0.47, 10, "no")],
                covered_until=300)
    r = run(con)
    assert sum(f.size for f in r.fills) == pytest.approx(10)


def test_blackout_start_cancels_resting_orders(tmp_path):
    # blackout begins at t=100 (3 days before close); the book never changes again
    con = build(tmp_path, books=[(0, YES_OK, NO_OK)], trades=[(1800, 0.40, 50, "no")],
                close_s=259200 + 100, covered_until=1800)
    r = run(con)
    assert r.fills == []
    assert sum(r.quoted_s_by_day.values()) == pytest.approx(100 - LAG)


def test_program_end_stops_quoting_and_rewards(tmp_path):
    con = build(tmp_path, books=[(0, YES_OK, NO_OK)], program_end_s=500, covered_until=1800)
    r = run(con)
    assert sum(r.quoted_s_by_day.values()) == pytest.approx(500 - LAG)
    assert sum(r.reward_by_day.values()) == pytest.approx(0.001 * 0.1 * (500 - LAG))
