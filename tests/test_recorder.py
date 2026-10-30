import contextlib
import json
from pathlib import Path

from kxmm import db
from kxmm.config import load_config
from kxmm.kalshi_public import NotFound, Unavailable
from kxmm.recorder import Recorder, _StopRequested
from tests.helpers import NO, NOW, YES, mk, raw_book

FIX = Path(__file__).parent / "fixtures"
CFG = load_config("config.yaml")


class FakeClient:
    def __init__(self, specs=(), book=None, trades=None):
        self.programs = [p for p, _, _ in specs]
        self.markets = {m["ticker"]: m for _, m, _ in specs}
        self.series_map = {s["ticker"]: s for _, _, s in specs}
        self.book = book or raw_book(YES, NO)
        self.trade_rows = trades or []
        self.fail = {}            # ticker -> exception to raise
        self.trade_calls = []     # (ticker, min_ts)
        self.book_calls = 0
        self.on_attempt = None    # wired by Recorder, like KalshiPublic

    def incentive_programs(self, status="active", type_="liquidity"):
        return list(self.programs)

    def markets_by_tickers(self, tickers, batch):
        return [self.markets[t] for t in tickers if t in self.markets]

    def series(self, ticker):
        return self.series_map.get(ticker, {})

    def orderbook(self, ticker):
        self.book_calls += 1
        if self.on_attempt:
            self.on_attempt()
        if ticker in self.fail:
            raise self.fail[ticker]
        return self.book

    def trades(self, ticker, min_ts, max_pages):
        if ticker in self.fail:
            raise self.fail[ticker]
        self.trade_calls.append((ticker, min_ts))
        return [dict(t, ticker=ticker) for t in self.trade_rows]


def recorder(tmp_path, client, clock=lambda: NOW, sleep=lambda s: None):
    con = db.connect(tmp_path / "out" / "kxmm.db")
    return Recorder(CFG, client, con, tmp_path, clock=clock, sleep=sleep,
                    awake=contextlib.nullcontext), con


def test_refresh_universe_selects_and_logs(tmp_path):
    c = FakeClient([mk("K1", "EK-1", 0.50, 10, 72, 50), mk("K2", "EL-1", 0.50, 10, 72, 40)])
    rec, con = recorder(tmp_path, c)
    assert sorted(rec.refresh_universe(NOW)) == ["K1", "K2"]
    assert con.execute("SELECT COUNT(*) FROM selections").fetchone()[0] == 1


def test_refresh_keeps_current_member_outside_shortlist(tmp_path, monkeypatch):
    c = FakeClient([mk("BIG", "EB-1", 0.50, 10, 72, 90), mk("SMALL", "ES-1", 0.50, 10, 72, 20)])
    rec, _ = recorder(tmp_path, c)
    monkeypatch.setitem(rec.rec, "shortlist_n", 1)       # SMALL ranks outside the shortlist
    rec.universe = ["SMALL"]
    assert "SMALL" in rec.refresh_universe(NOW)


def test_refresh_keeps_current_member_when_its_book_fetch_fails(tmp_path):
    c = FakeClient([mk(f"K{i}", f"E{i}-1", 0.50, 10, 72, 50 - i) for i in range(6)])
    c.fail["K5"] = Unavailable("blip")
    rec, _ = recorder(tmp_path, c)
    rec.universe = ["K5"]
    assert "K5" in rec.refresh_universe(NOW)


def test_refresh_fails_whole_when_most_fetches_fail(tmp_path):
    import pytest
    c = FakeClient([mk(f"K{i}", f"E{i}-1", 0.50, 10, 72, 50) for i in range(5)])
    for i in range(5):
        c.fail[f"K{i}"] = Unavailable("network down")
    rec, con = recorder(tmp_path, c)
    rec.universe = ["OLD1", "OLD2"]
    with pytest.raises(RuntimeError):
        rec.refresh_universe(NOW)
    assert rec.universe == ["OLD1", "OLD2"]
    assert con.execute("SELECT COUNT(*) FROM selections").fetchone()[0] == 0


def test_poll_books_stores_changes_only(tmp_path):
    t = {"now": NOW}
    rec, con = recorder(tmp_path, FakeClient(), clock=lambda: t["now"])
    rec.universe = ["K1"]
    rec.poll_books(NOW)
    t["now"] = NOW + 10
    rec.poll_books(NOW + 10)
    assert con.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 1
    assert con.execute("SELECT last_ok FROM poll_state WHERE ticker='K1'").fetchone()[0] == NOW + 10


def test_poll_books_survives_one_failing_market(tmp_path):
    c = FakeClient()
    c.fail["A"] = Unavailable("503s")
    rec, con = recorder(tmp_path, c)
    rec.universe = ["A", "B"]
    rec.poll_books(NOW)
    assert [r[0] for r in con.execute("SELECT DISTINCT ticker FROM books")] == ["B"]
    assert con.execute("SELECT COUNT(*) FROM poll_state WHERE ticker='A'").fetchone()[0] == 0


def test_poll_books_drops_market_on_not_found(tmp_path):
    c = FakeClient()
    c.fail["A"] = NotFound("A")
    rec, _ = recorder(tmp_path, c)
    rec.universe = ["A", "B"]
    rec.poll_books(NOW)
    assert rec.universe == ["B"]


def test_poll_trades_advances_cursor_ts(tmp_path):
    rows = json.loads((FIX / "trades.json").read_text(encoding="utf-8"))["trades"]
    c = FakeClient(trades=rows)
    rec, con = recorder(tmp_path, c)
    rec.universe = ["K1"]
    rec.poll_trades(NOW)
    newest = con.execute("SELECT MAX(ts) FROM trades WHERE ticker='K1'").fetchone()[0]
    rec.poll_trades(NOW + 30)
    assert c.trade_calls[0] == ("K1", NOW - CFG["record"]["trades_backfill_s"] - 1)
    assert c.trade_calls[1] == ("K1", int(newest) - 1)


def test_run_stops_mid_refresh_when_kill_appears(tmp_path):
    class KillOnFirstBook(FakeClient):
        calls = 0

        def orderbook(self, ticker):
            KillOnFirstBook.calls += 1
            (tmp_path / "out" / "KILL").write_text("", encoding="utf-8")
            return super().orderbook(ticker)

    c = KillOnFirstBook([mk(f"K{i}", f"E{i}-1", 0.50, 10, 72, 50) for i in range(5)])
    rec, con = recorder(tmp_path, c)
    rec.run()                                     # a 99 s refresh must not delay stop
    assert KillOnFirstBook.calls == 1
    assert not (tmp_path / "out" / "KILL").exists()
    assert db.kv_get(con, "stopped") is not None


def test_run_exits_on_kill_file(tmp_path):
    t = {"now": float(NOW), "slices_after_kill": None}
    kill = tmp_path / "out" / "KILL"

    def clock():
        return t["now"]

    def sleep(s):
        t["now"] += s
        if t["slices_after_kill"] is None:
            kill.parent.mkdir(parents=True, exist_ok=True)
            kill.write_text("", encoding="utf-8")
            t["slices_after_kill"] = 0
        else:
            t["slices_after_kill"] += 1
        assert t["slices_after_kill"] < 2, "run() did not notice KILL"

    rec, con = recorder(tmp_path, FakeClient(), clock=clock, sleep=sleep)
    rec.run()
    assert not kill.exists()
    assert db.last_heartbeat(con) is not None
    assert float(db.kv_get(con, "stopped")) >= db.last_heartbeat(con)


def test_poll_books_stops_pass_after_three_failures(tmp_path):
    c = FakeClient()
    rec, _ = recorder(tmp_path, c)
    rec.universe = [f"T{i}" for i in range(10)]
    for t in rec.universe:
        c.fail[t] = Unavailable("network down")
    rec.poll_books(NOW)
    assert c.book_calls == 3


def test_failing_requests_keep_heartbeat_fresh_and_honor_kill(tmp_path):
    import pytest
    t = {"now": float(NOW)}

    class SlowFail(FakeClient):
        def orderbook(self, ticker):
            t["now"] += 87                       # every attempt times out
            return super().orderbook(ticker)

    c = SlowFail()
    rec, con = recorder(tmp_path, c, clock=lambda: t["now"])
    rec.universe = ["A", "B", "C"]
    for x in rec.universe:
        c.fail[x] = Unavailable("timeout")
    rec.poll_books(NOW)
    assert db.last_heartbeat(con) == t["now"]     # beat after each failed attempt
    (tmp_path / "out" / "KILL").write_text("", encoding="utf-8")
    c.book_calls = 0
    with pytest.raises(_StopRequested):
        rec.poll_books(NOW)
    assert c.book_calls == 1                      # stop honoured within one request
