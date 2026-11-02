import contextlib

from kxmm import tradedb
from kxmm.config import load_config
from kxmm.drills import run_drill
from kxmm.trader import Trader
from tests.fakes import FakeTradeClient, market
from tests.helpers import NO, YES, raw_book

T = 1_800_000_000.0
CFG = load_config()
CFG["trade"] = dict(CFG["trade"], markets=["K"])


class Clock:
    def __init__(self):
        self.t = T

    def __call__(self):
        return self.t


def standalone(tmp_path, book=None):
    clock = Clock()
    fake = FakeTradeClient(books={"K": book if book is not None else raw_book(YES, NO)},
                           markets=[market("K")], now=clock)
    con = tradedb.connect(tmp_path / "out" / "trade" / "trade.db")

    def sleep(s):
        clock.t += s
    return fake, con, clock, sleep


def looped(tmp_path):
    clock = Clock()
    fake = FakeTradeClient(books={"K": raw_book(YES, NO)}, markets=[market("K")], now=clock,
                           fault_dir=tmp_path / "out" / "trade")
    con = tradedb.connect(tmp_path / "out" / "trade" / "trade.db")
    tr = Trader(CFG, fake, con, tmp_path, clock=clock, sleep=lambda s: None,
                awake=contextlib.nullcontext)
    tr.start()
    tr.tick()

    def sleep(s):                     # the loop runs alongside the drill
        clock.t += s
        if not tr.stopped:
            tr.tick()
    return tr, fake, con, clock, sleep


def kinds(con):
    return [r[0] for r in con.execute("SELECT kind FROM events ORDER BY id")]


def test_orders_drill_passes(tmp_path):
    fake, con, clock, sleep = standalone(tmp_path)
    ok, detail = run_drill("orders", fake, con, tmp_path, CFG, clock, sleep)
    assert ok, detail
    assert [c[0] for c in fake.calls] == ["create", "amend", "amend", "cancel"]
    assert fake.orders == {}
    assert con.execute("SELECT name, passed FROM drills").fetchone()[:] == ("orders", 1)


def test_group_drill_passes(tmp_path):
    fake, con, clock, sleep = standalone(tmp_path)
    ok, detail = run_drill("group", fake, con, tmp_path, CFG, clock, sleep)
    assert ok, detail
    assert fake.orders == {} and all(not g["triggered"] for g in fake.groups.values())


def test_fill_drill_passes(tmp_path):
    fake, con, clock, sleep = standalone(tmp_path)        # NO bids at 0.49 -> YES ask 0.51
    ok, detail = run_drill("fill", fake, con, tmp_path, CFG, clock, sleep)
    assert ok, detail
    assert fake.pos["K"] == 1 and ("take", "K", "bid", 0.51, 1) in fake.calls


def test_reconcile_drill_passes(tmp_path):
    tr, fake, con, clock, sleep = looped(tmp_path)
    ok, detail = run_drill("reconcile", fake, con, tmp_path, CFG, clock, sleep)
    assert ok, detail
    assert "reconcile_drill" in kinds(con) and "reconcile_bad" not in kinds(con)
    assert not (tmp_path / "out" / "trade" / "HALT").exists()


def test_kill_drill_passes(tmp_path):
    tr, fake, con, clock, sleep = looped(tmp_path)
    ok, detail = run_drill("kill", fake, con, tmp_path, CFG, clock, sleep)
    assert ok, detail
    assert tr.stopped and fake.orders == {}


def test_network_drill_passes(tmp_path):
    tr, fake, con, clock, sleep = looped(tmp_path)
    ok, detail = run_drill("network", fake, con, tmp_path, CFG, clock, sleep)
    assert ok, detail
    assert "outage" in kinds(con) and "error" not in kinds(con)
    assert not (tmp_path / "out" / "trade" / "FAULT_NET").exists()
