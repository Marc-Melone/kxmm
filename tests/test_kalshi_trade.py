import pytest
import requests

from kxmm.kalshi_public import Unavailable
from kxmm.kalshi_trade import DEMO_BASES, KalshiTrade, TradeError

BASE = DEMO_BASES[0]
SECRET_ID, SECRET_SIG = "KEYID-SECRET-123", "SIG-SECRET-456"


class FakeSigner:
    def __init__(self):
        self.paths = []

    def headers(self, method, path, ts_ms=None):
        self.paths.append((method, path))
        return {"KALSHI-ACCESS-KEY": SECRET_ID, "KALSHI-ACCESS-TIMESTAMP": "1",
                "KALSHI-ACCESS-SIGNATURE": SECRET_SIG}


class Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body if body is not None else {}

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params or {}, "json": json})
        return Resp(*self.responses.pop(0))


def client(responses, fault_dir=None):
    s, sg = FakeSession(responses), FakeSigner()
    return KalshiTrade(BASE, sg, rate_per_sec=1000, session=s, sleep=lambda x: None,
                       fault_dir=fault_dir), s, sg


def test_refuses_production_base_url():
    for url in ("https://external-api.kalshi.com/trade-api/v2",
                "https://api.elections.kalshi.com/trade-api/v2"):
        with pytest.raises(ValueError):
            KalshiTrade(url, FakeSigner())


def test_create_order_body():
    c, s, _ = client([(201, {"order_id": "o1"})])
    assert c.create_order("K", "ask", 0.52, 10, "cid", "g1")["order_id"] == "o1"
    call = s.calls[0]
    assert call["method"] == "POST" and call["url"] == BASE + "/portfolio/events/orders"
    assert call["json"] == {"ticker": "K", "side": "ask", "price": "0.5200", "count": "10.00",
                            "time_in_force": "good_till_canceled", "post_only": True,
                            "cancel_order_on_pause": True,
                            "self_trade_prevention_type": "taker_at_cross",
                            "client_order_id": "cid", "order_group_id": "g1"}


def test_signed_path_includes_prefix_not_query():
    c, _, sg = client([(200, {"fills": [], "cursor": ""})])
    c.fills(min_ts=5)
    assert sg.paths == [("GET", "/trade-api/v2/portfolio/fills")]


def test_batch_cancel_and_amend_bodies():
    c, s, _ = client([(200, {"orders": []}), (200, {"order_id": "o1", "ts_ms": 1})])
    c.batch_cancel([("o1", "K"), ("o2", "K")])
    c.amend_order("o1", "K", "bid", 0.49, 7)
    assert (s.calls[0]["method"], s.calls[0]["url"]) == ("DELETE",
                                                         BASE + "/portfolio/events/orders/batched")
    # market_ticker routes each cancel to the right exchange shard
    assert s.calls[0]["json"] == {"orders": [{"order_id": "o1", "market_ticker": "K"},
                                             {"order_id": "o2", "market_ticker": "K"}]}
    assert (s.calls[1]["method"], s.calls[1]["url"]) == ("POST",
                                                         BASE + "/portfolio/events/orders/o1/amend")
    assert s.calls[1]["json"] == {"ticker": "K", "side": "bid", "price": "0.4900", "count": "7.00"}


def test_fills_paginates():
    c, s, _ = client([(200, {"fills": [{"fill_id": "a"}], "cursor": "c1"}),
                      (200, {"fills": [{"fill_id": "b"}], "cursor": ""})])
    assert [f["fill_id"] for f in c.fills(min_ts=0)] == ["a", "b"]
    assert s.calls[1]["params"]["cursor"] == "c1"


def test_retries_429_then_succeeds():
    c, s, _ = client([(429, {}), (200, {"trading_active": True})])
    assert c.exchange_status()["trading_active"] is True and len(s.calls) == 2


def test_401_hints_clock():
    c, _, _ = client([(401, {"code": "unauthorized"})])
    with pytest.raises(TradeError, match="clock"):
        c.positions()


def test_errors_never_contain_credentials(tmp_path):
    for responses in ([(401, {"message": "bad sig"})], [(500, {})] * 4):
        c, _, _ = client(responses)
        with pytest.raises(Exception) as e:
            c.positions()
        text = str(e.value) + repr(e.value)
        assert SECRET_ID not in text and SECRET_SIG not in text


def test_fault_files_only_on_demo(tmp_path):
    c, s, _ = client([], fault_dir=tmp_path)
    assert c.is_demo
    (tmp_path / "FAULT_NET").write_text("")
    with pytest.raises(Unavailable):
        c.exchange_status()
    (tmp_path / "FAULT_NET").unlink()
    (tmp_path / "FAULT_429").write_text("")
    with pytest.raises(Unavailable):
        c.exchange_status()
    assert s.calls == []                       # faults never reach the network


def test_batch_cancel_returns_per_order_failures():
    c, _, _ = client([(200, {"orders": [{"order_id": "o1", "reduced_by": "1.00"},
                                        {"order_id": "o2", "reduced_by": "0.00",
                                         "error": {"code": "not_found", "message": "gone"}}]})])
    assert c.batch_cancel([("o1", "K"), ("o2", "K")]) == ["o2"]


def test_order_post_is_not_retried_after_ambiguous_failure():
    c, s, _ = client([(503, {})])
    with pytest.raises(Unavailable, match="may have reached"):
        c.create_order("K", "bid", 0.03, 1, "cid", None)
    assert len(s.calls) == 1                       # never re-sent: it may have landed
    c, s, _ = client([(429, {}), (201, {"order_id": "o1"})])
    assert c.create_order("K", "bid", 0.03, 1, "cid", None)["order_id"] == "o1"   # 429 is safe
