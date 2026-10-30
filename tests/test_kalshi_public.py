import json
from pathlib import Path

import pytest

from kxmm.kalshi_public import KalshiPublic, NotFound, Unavailable

FIX = Path(__file__).parent / "fixtures"
BASE = "https://api.elections.kalshi.com/trade-api/v2"


def fixture(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """Serves queued (status, body) responses; records each request."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def get(self, url, params=None, timeout=None):
        self.requests.append((url, dict(params or {})))
        if not self.responses:
            raise AssertionError("unexpected extra request")
        return Resp(*self.responses.pop(0))


def client(responses, sleeps=None):
    sleeps = [] if sleeps is None else sleeps
    s = FakeSession(responses)
    c = KalshiPublic(BASE, rate_per_sec=1000, timeout_s=5, max_attempts=4,
                     session=s, sleep=sleeps.append)
    return c, s


def test_incentive_programs_follows_next_cursor():
    page1 = fixture("incentive_programs.json")
    c, s = client([(200, page1), (200, {"incentive_programs": []})])
    rows = c.incentive_programs()
    assert len(rows) == 3
    assert s.requests[1][1]["cursor"] == page1["next_cursor"]
    assert s.requests[0][1]["type"] == "liquidity" and s.requests[0][1]["status"] == "active"


def test_markets_by_tickers_batches():
    tickers = [f"T{i}" for i in range(120)]
    c, s = client([(200, {"markets": []})] * 3)
    c.markets_by_tickers(tickers, batch=50)
    assert len(s.requests) == 3
    assert len(s.requests[0][1]["tickers"].split(",")) == 50


def test_orderbook_returns_fp_object():
    c, _ = client([(200, fixture("orderbook.json"))])
    ob = c.orderbook("KXSTREAMARRAY-26NOV08-T80")
    assert ob["yes_dollars"][-1] == ["0.9500", "44.00"]


def test_orderbook_404_raises_not_found():
    c, _ = client([(404, {"error": "not found"})])
    with pytest.raises(NotFound):
        c.orderbook("GONE")


def test_retries_429_then_succeeds():
    sleeps = []
    c, _ = client([(429, {}), (200, fixture("series.json"))], sleeps)
    assert c.series("KXSTREAMARRAY")["category"] == "Financials"
    assert sleeps.count(1) == 1


def test_gives_up_after_max_attempts():
    c, _ = client([(503, {})] * 4)
    with pytest.raises(Unavailable):
        c.series("KXSTREAMARRAY")


def test_trades_paginates_until_empty_cursor():
    t = fixture("trades.json")["trades"][0]
    pages = [(200, {"trades": [t, t], "cursor": "c1"}),
             (200, {"trades": [t, t], "cursor": "c2"}),
             (200, {"trades": [t], "cursor": ""})]
    c, s = client(pages)
    rows = c.trades("KXCPIYOY-26NOV-T3.0", min_ts=1_800_000_000, max_pages=20)
    assert len(rows) == 5
    assert all(p["min_ts"] == 1_800_000_000 for _, p in s.requests)
    assert s.requests[2][1]["cursor"] == "c2"


def test_trades_stops_at_max_pages():
    t = fixture("trades.json")["trades"][0]
    c, s = client([(200, {"trades": [t], "cursor": f"c{i}"}) for i in range(10)])
    c.trades("X", min_ts=0, max_pages=3)
    assert len(s.requests) == 3


def test_rejects_unknown_base_url():
    with pytest.raises(ValueError):
        KalshiPublic("https://evil.example/trade-api/v2", 8, 5, 4, session=FakeSession([]))


def test_client_calls_on_attempt_after_every_try():
    calls = []
    c, _ = client([(503, {}), (200, fixture("series.json"))])
    c.on_attempt = lambda: calls.append(1)
    c.series("KXSTREAMARRAY")
    assert len(calls) == 2                      # the failed try counts too
