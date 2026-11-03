import contextlib

import pytest

from kxmm import tradedb
from kxmm.config import load_config
from kxmm.trader import Trader
from tests.fakes import FakeTradeClient, market
from kxmm.kalshi_public import Unavailable
from tests.helpers import NO, YES, raw_book

T = 1_800_000_000.0          # 2027-01-15 08:00 UTC
CFG = load_config()
CFG["trade"] = dict(CFG["trade"], markets=["K"])
TC = CFG["trade"]


class Clock:
    def __init__(self, t=T):
        self.t = t

    def __call__(self):
        return self.t


def make(tmp_path, book=None, clock=None, cfg=None, markets=None, books=None):
    clock = clock or Clock()
    fake = FakeTradeClient(books=books or {"K": book if book is not None else raw_book(YES, NO)},
                           markets=markets or [market("K")], now=clock,
                           fault_dir=tmp_path / "out" / "trade")
    con = tradedb.connect(tmp_path / "out" / "trade" / "trade.db")
    tr = Trader(cfg or CFG, fake, con, tmp_path, clock=clock, sleep=lambda s: None,
                awake=contextlib.nullcontext)
    return tr, fake, con, clock


def sides(fake, ticker="K"):
    return sorted((o["book_side"], o["price"]) for o in fake.orders.values()
                  if o["ticker"] == ticker)


def events(con, kind):
    return [r["detail"] for r in con.execute("SELECT detail FROM events WHERE kind=?", (kind,))]


def test_start_cancels_everything_first(tmp_path):
    tr, fake, _, _ = make(tmp_path)
    fake.orders["old"] = {"order_id": "old", "ticker": "Z", "book_side": "bid", "price": 0.1,
                          "remaining": 1.0, "filled": 0.0, "group": None}
    tr.start()
    assert fake.orders == {} and fake.calls[0] == ("cancel_all",)


def test_places_two_sided_quotes_on_a_live_book(tmp_path):
    tr, fake, _, clock = make(tmp_path)
    tr.start()
    tr.cycle(clock())
    # YES bid at the 0.50 reference; NO bid 0.49 placed as a YES ask at 0.51
    assert sides(fake) == [("ask", 0.51), ("bid", 0.50)]


def test_plumbing_quotes_on_empty_book_and_churn(tmp_path):
    tr, fake, _, clock = make(tmp_path, book={"yes_dollars": [], "no_dollars": []})
    tr.start()
    tr.cycle(clock())
    assert sides(fake) == [("ask", 0.97), ("bid", 0.03)]
    for k in range(1, 7):                             # cycling keeps the expiry fresh
        clock.t = T + 100 * k
        tr.cycle(clock())
    assert sides(fake) == [("ask", 0.96), ("bid", 0.04)]     # churned a tick at T+600
    assert [c[0] for c in fake.calls].count("create") == 2   # by amend, never re-placed


def test_fill_updates_position_and_requotes(tmp_path):
    tr, fake, con, clock = make(tmp_path)
    tr.start()
    tr.cycle(clock())
    bid = next(o for o, v in fake.orders.items() if v["book_side"] == "bid")
    fake.fill(bid, 1)
    clock.t += 5
    tr.cycle(clock())
    assert tr.pnl.position("K") == pytest.approx(1)
    assert any(v["book_side"] == "bid" for v in fake.orders.values())   # re-placed
    clock.t += TC["reconcile_s"]
    tr.cycle(clock())
    assert events(con, "reconcile_bad") == [] and events(con, "reconcile_ok")


def test_reconcile_mismatch_halts_and_cancels(tmp_path):
    tr, fake, con, clock = make(tmp_path)
    tr.start()
    tr.cycle(clock())
    fake.misreport["K"] = 5
    clock.t += TC["reconcile_s"]
    tr.cycle(clock())
    halt = tmp_path / "out" / "trade" / "HALT"
    assert halt.exists() and "position" in halt.read_text(encoding="utf-8")
    assert fake.orders == {} and events(con, "reconcile_bad")


def test_daily_loss_pauses_until_next_day(tmp_path):
    tr, fake, con, clock = make(tmp_path)
    tr.start()
    tr.cycle(clock())
    tr.pnl.cash -= 9.0                                   # 3% of the $300 stake
    clock.t += 5
    tr.cycle(clock())
    assert fake.orders == {} and events(con, "pause")
    clock.t += 3600
    tr.cycle(clock())
    assert fake.orders == {}                             # still today
    clock.t = (T // 86400 + 1) * 86400 + 1               # next UTC day
    tr.cycle(clock())
    assert sides(fake) == [("ask", 0.51), ("bid", 0.50)]


def test_stale_book_cancels_that_market(tmp_path):
    tr, fake, con, clock = make(tmp_path)
    tr.start()
    tr.cycle(clock())
    fake.fail_book.add("K")
    clock.t += TC["stale_book_s"] + 1
    tr.cycle(clock())
    assert fake.orders == {} and events(con, "stale")


def test_kill_cancels_all_and_exits(tmp_path):
    clock = Clock()
    tr, fake, con, _ = make(tmp_path, clock=clock)
    kill = tmp_path / "out" / "trade" / "KILL"

    def sleep(s):
        clock.t += s
        kill.write_text("", encoding="utf-8")

    tr.sleep = sleep
    tr.run()
    assert fake.orders == {} and not kill.exists()
    assert tradedb.kv_get(con, "stopped") is not None and events(con, "stop") is not None


def test_action_error_is_recorded_not_fatal(tmp_path):
    tr, fake, con, clock = make(tmp_path)
    fake.fail_create.add("K")
    fake.fail_create_msg = "HTTP 400 on POST /portfolio/events/orders: invalid_price"
    tr.start()
    tr.cycle(clock())
    assert events(con, "error") and "invalid_price" in events(con, "error")[0]


def test_races_are_rejects_not_errors(tmp_path):
    tr, fake, con, clock = make(tmp_path)
    fake.fail_create.add("K")                        # post-only reject: the book moved
    tr.start()
    tr.cycle(clock())
    fake.fail_create.clear()
    clock.t += 5
    tr.cycle(clock())
    bid = next(o for o, v in fake.orders.items() if v["book_side"] == "bid")
    fake.orders.pop(bid)                             # filled away just before our amend
    tr._quote("K", tr.meta["K"], clock(), [{"order_id": bid, "ticker": "K", "book_side": "bid",
             "yes_price_dollars": "0.4000", "remaining_count_fp": "1.00",
             "fill_count_fp": "0.00"}], tr.marks())
    assert events(con, "error") == [] and len(events(con, "reject")) == 3   # 2 places + 1 amend


def test_no_plumbing_inside_blackout_or_off_demo(tmp_path):
    tr, fake, con, clock = make(tmp_path, book={"yes_dollars": [["0.4000", "5.00"]],
                                                "no_dollars": []})
    fake.market_rows = [market("K", close_days=2)]  # inside the 3-day blackout
    tr.start()
    tr.cycle(clock())
    assert fake.orders == {}
    (tmp_path / "x").mkdir()
    tr2, fake2, _, clock2 = make(tmp_path / "x", book={"yes_dollars": [], "no_dollars": []})
    fake2.is_demo = False
    tr2.start()
    tr2.cycle(clock2())
    assert fake2.orders == {}


def test_order_from_an_ambiguous_create_is_adopted_not_foreign(tmp_path):
    tr, fake, con, clock = make(tmp_path)
    fake.ambiguous_create = True
    tr.start()
    tr.cycle(clock())                                # creates land but the calls "time out"
    fake.ambiguous_create = False
    clock.t += TC["reconcile_s"]
    tr.cycle(clock())
    assert events(con, "outage") and events(con, "reconcile_bad") == []
    assert len(fake.orders) == 2                     # adopted, not doubled


def test_failed_cancel_is_kept_and_retried(tmp_path):
    tr, fake, con, clock = make(tmp_path)
    tr.start()
    tr.cycle(clock())
    fake.fail_cancel = set(fake.orders)
    fake.fail_book.add("K")
    clock.t += TC["stale_book_s"] + 1
    tr.cycle(clock())                                # stale: cancel fails
    assert len(fake.orders) == 2
    fake.fail_cancel.clear()
    clock.t += 5
    tr.cycle(clock())                                # retried, not forgotten
    assert fake.orders == {}



def uptime(con):
    return con.execute("SELECT COUNT(*) FROM uptime").fetchone()[0]


def test_only_completed_trading_cycles_count_as_uptime(tmp_path):
    tr, fake, con, clock = make(tmp_path)
    tdir = tmp_path / "out" / "trade"
    tr.start()
    tr.tick()
    assert uptime(con) == 1
    (tdir / "FAULT_NET").write_text("")          # offline: cycles fail
    clock.t += 120
    tr.tick()
    (tdir / "FAULT_NET").unlink()
    (tdir / "HALT").write_text("x")              # halted: no trading
    clock.t += 120
    tr.tick()
    assert uptime(con) == 1


def test_restart_keeps_daily_pause(tmp_path):
    tr, fake, con, clock = make(tmp_path)
    tr.start()
    tr.cycle(clock())
    tr.pnl.cash -= 9.0
    clock.t += 5
    tr.cycle(clock())
    assert events(con, "pause")
    again = Trader(CFG, fake, con, tmp_path, clock=clock, sleep=lambda s: None,
                   awake=contextlib.nullcontext)
    again.start()
    clock.t += 5
    again.cycle(clock())
    assert fake.orders == {}                              # still paused after the restart
    assert again.pnl.cash == pytest.approx(-9.0)


def test_missing_mark_uses_last_known_then_worst_case(tmp_path):
    tr, fake, con, clock = make(tmp_path)
    tr.start()
    tr.cycle(clock())                                     # K's mid 0.505 becomes known
    fake.books["K"] = raw_book(YES, [])                   # NO side vanishes: no mid
    tr.pnl.apply_fill("K", "ask", 0.50, 25, 0.0)          # short 25 YES
    tr.pnl.apply_fill("Z", "bid", 0.40, 2, 0.0)           # long 2 of a never-priced market
    tr.pnl.apply_fill("Y", "ask", 0.40, 2, 0.0)           # short 2 of a never-priced market
    clock.t += 5
    tr.cycle(clock())
    marks = tr.marks()
    assert marks["K"] == pytest.approx(0.505)             # last known, not 0
    assert marks["Z"] == 0.0 and marks["Y"] == 1.0        # worst case for each direction


def test_clearing_a_weekly_halt_rebases_the_week(tmp_path):
    tr, fake, con, clock = make(tmp_path)
    tr.start()
    tr.cycle(clock())
    tr.pnl.cash -= 24.0                                   # 8% of $300 this week
    clock.t += 5
    tr.cycle(clock())
    halt = tmp_path / "out" / "trade" / "HALT"
    assert halt.exists()
    halt.unlink()                                         # a person reviewed it
    clock.t = (T // 86400 + 1) * 86400 + 1                # next day, same week
    tr.cycle(clock())
    tr.cycle(clock() + 5)
    assert not halt.exists() and sides(fake) == [("ask", 0.51), ("bid", 0.50)]


def test_start_does_not_double_count_recent_fills(tmp_path):
    tr, fake, con, clock = make(tmp_path)
    clock.t -= 30
    fake.fill_rows.append({"fill_id": "early", "ticker": "K", "book_side": "bid",
                           "yes_price_dollars": "0.5100", "count_fp": "1.00", "fee_cost": "0",
                           "order_id": "taker", "created_time": __import__("tests.helpers",
                           fromlist=["iso"]).iso(clock())})
    fake.pos["K"] = 1.0                                   # e.g. the fill drill just ran
    clock.t += 30
    tr.start()
    tr.cycle(clock())
    clock.t += TC["reconcile_s"]
    tr.cycle(clock())
    assert tr.pnl.position("K") == pytest.approx(1)
    assert events(con, "reconcile_bad") == []
    assert tr.pnl.cash == pytest.approx(-0.51)            # its cost still counts


def test_orders_expire_on_their_own_and_are_refreshed(tmp_path):
    tr, fake, con, clock = make(tmp_path)
    tr.start()
    tr.cycle(clock())
    ttl = TC["order_ttl_s"]
    assert all(o["expiration"] == clock() + ttl for o in fake.orders.values())
    clock.t += ttl / 2 + 1
    tr.cycle(clock())
    assert [c[0] for c in fake.calls].count("amend") == 2          # both expiries refreshed
    assert all(o["expiration"] > clock() + ttl / 2 for o in fake.orders.values())


def test_kill_during_outage_exits_and_relies_on_expiry(tmp_path):
    tr, fake, con, clock = make(tmp_path)
    tr.start()
    tr.tick()
    tdir = tmp_path / "out" / "trade"
    (tdir / "FAULT_NET").write_text("")
    (tdir / "KILL").write_text("")
    assert tr.tick() is False
    assert any("expire" in d for d in events(con, "outage"))


def test_halt_cancel_is_retried_until_it_lands(tmp_path):
    tr, fake, con, clock = make(tmp_path)
    tr.start()
    tr.cycle(clock())
    real, fails = fake.cancel_all, [1]

    def flaky():
        if fails:
            fails.pop()
            raise Unavailable("network down")
        real()
    fake.cancel_all = flaky
    (tmp_path / "out" / "trade" / "HALT").write_text("x")
    tr.cycle(clock())
    assert fake.orders                                  # the first cancel failed
    tr.cycle(clock() + 5)
    assert fake.orders == {}                            # retried and confirmed


def test_run_cancels_everything_even_on_ctrl_c(tmp_path):
    tr, fake, con, clock = make(tmp_path)

    def interrupt(s):
        raise KeyboardInterrupt
    tr.sleep = interrupt
    with pytest.raises(KeyboardInterrupt):
        tr.run()
    assert fake.orders == {}


def test_tripped_group_is_reset_after_cooldown(tmp_path):
    tr, fake, con, clock = make(tmp_path)
    tr.start()
    tr.cycle(clock())
    fake.trigger_order_group(tr.groups["K"])            # a burst of fills: Kalshi pulls all
    clock.t += 5
    tr.cycle(clock())
    assert events(con, "group_trip") and fake.orders == {}
    clock.t += TC["group_cooldown_s"] + 1
    tr.cycle(clock())
    assert events(con, "group_reset") and sides(fake) == [("ask", 0.51), ("bid", 0.50)]


def test_repeated_group_trips_halt(tmp_path):
    tr, fake, con, clock = make(tmp_path)
    tr.start()
    tr.cycle(clock())
    for _ in range(TC["max_group_resets_per_hour"] + 1):
        fake.trigger_order_group(tr.groups["K"])
        clock.t += 5
        tr.cycle(clock())
        clock.t += TC["group_cooldown_s"] + 1
        tr.cycle(clock())
    halt = tmp_path / "out" / "trade" / "HALT"
    assert halt.exists() and "order group" in halt.read_text(encoding="utf-8")


def test_settled_position_is_booked_not_a_mismatch(tmp_path):
    tr, fake, con, clock = make(tmp_path)
    tr.start()
    tr.cycle(clock())
    bid = next(o for o, v in fake.orders.items() if v["book_side"] == "bid")
    fake.fill(bid, 1)                                   # long 1 YES at 0.50
    clock.t += 5
    tr.cycle(clock())
    cash = tr.pnl.cash
    fake.settled.add("K")
    fake.market_rows = [market("K", status="finalized", result="yes")]
    clock.t += TC["reconcile_s"]
    tr.cycle(clock())
    assert events(con, "settle") and events(con, "reconcile_bad") == []
    assert tr.pnl.position("K") == 0 and tr.pnl.cash == pytest.approx(cash + 1.0)


def auto_cfg(**trade):
    cfg = load_config()
    cfg["trade"] = dict(cfg["trade"], markets=[], **trade)
    return cfg


def test_closed_or_blacked_out_market_is_replaced(tmp_path):
    books = {"K": raw_book(YES, NO), "J": raw_book(YES, NO)}
    tr, fake, con, clock = make(tmp_path, cfg=auto_cfg(max_markets=1), books=books,
                                markets=[market("K", close_days=15), market("J", close_days=40)])
    tr.start()
    assert list(tr.meta) == ["K"]
    clock.t = T + (15 - 3) * 86400 + 1                  # K enters its 3-day blackout
    tr.cycle(clock())
    assert list(tr.meta) == ["J"] and sides(fake, "J") == [("ask", 0.51), ("bid", 0.50)]


def test_auto_pick_skips_early_closers_and_short_dated(tmp_path):
    rows = [market("A", close_days=30, can_close_early=True), market("B", close_days=5),
            market("C", close_days=30)]
    tr, fake, con, clock = make(tmp_path, cfg=auto_cfg(), markets=rows)
    tr.start()
    assert list(tr.meta) == ["C"]
