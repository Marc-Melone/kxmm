from datetime import datetime, timezone

import pytest

from kxmm.book import Book
from kxmm.config import load_config
from kxmm.select import Candidate, Scored, choose, prefilter, score

NOW = 1_800_000_000
CENT = [{"start": "0.0000", "end": "1.0000", "step": "0.0100"}]
CFG = load_config("config.yaml")


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mk(ticker, event, mid, close_days, program_hours_left, pool_usd,
       fee_type="quadratic", mve=None, market_type="binary"):
    start, end = NOW - 86400, NOW + program_hours_left * 3600
    days = (end - start) / 86400
    program = {"id": f"p-{ticker}-{pool_usd}", "market_ticker": ticker,
               "start_date": iso(start), "end_date": iso(end),
               "period_reward": round(pool_usd * days * 10_000),
               "target_size_fp": "1000.00", "discount_factor_bps": 5000,
               "incentive_type": "liquidity"}
    market = {"ticker": ticker, "event_ticker": event, "status": "active",
              "market_type": market_type, "mve_collection_ticker": mve,
              "yes_bid_dollars": f"{mid - 0.01:.4f}", "yes_ask_dollars": f"{mid + 0.01:.4f}",
              "close_time": iso(NOW + close_days * 86400), "price_ranges": CENT}
    series = {"ticker": event.split("-")[0], "fee_type": fee_type, "category": "X"}
    return program, market, series


def build(*specs):
    progs, mkts, sers = [], {}, {}
    for p, m, s in specs:
        progs.append(p)
        mkts[m["ticker"]] = m
        sers[s["ticker"]] = s
    return progs, mkts, sers


def test_prefilter_excludes_by_rule():
    progs, mkts, sers = build(
        mk("A", "EA-1", 0.95, 10, 72, 50),
        mk("B", "EB-1", 0.50, 2, 72, 50),
        mk("C", "EC-1", 0.50, 10, 10, 50),
        mk("D", "ED-1", 0.50, 10, 72, 50, market_type="scalar"),
        mk("E", "EE-1", 0.50, 10, 72, 50, mve="KXMVE-R"),
        mk("K", "EK-1", 0.50, 10, 72, 50))
    assert [c.ticker for c in prefilter(progs, mkts, sers, NOW, CFG)] == ["K"]


def test_prefilter_takes_largest_program_per_ticker():
    p1, m, s = mk("K", "EK-1", 0.50, 10, 72, 14)
    p2, _, _ = mk("K", "EK-1", 0.50, 10, 72, 50)
    cands = prefilter([p1, p2], {"K": m}, {s["ticker"]: s}, NOW, CFG)
    assert len(cands) == 1
    assert cands[0].daily_pool_usd == pytest.approx(50, rel=1e-6)


YES = [(0.50, 100), (0.49, 150), (0.48, 300), (0.47, 1000)]
NO = [(0.49, 100), (0.48, 150), (0.47, 300), (0.46, 1000)]   # YES ask 0.51


def cand(pool=100.0, maker_fee=False, mid=0.5):
    return Candidate(ticker="K", event_ticker="EK-1", series_ticker="EK",
                     daily_pool_usd=pool, target_size=1000, discount=0.5, mid=mid,
                     close_ts=NOW + 10 * 86400, maker_fee=maker_fee, price_ranges=CENT)


def test_score_rejects_thin_side():
    assert score(cand(), Book(yes=YES, no=[(0.49, 400), (0.48, 500)]), CFG) is None


def test_score_values():
    s = score(cand(), Book(yes=YES, no=NO), CFG)
    assert s.share_yes == pytest.approx(10 / 660) and s.share_no == pytest.approx(10 / 660)
    assert s.est_daily_usd == pytest.approx(100 * 10 / 660)
    assert s.collateral_usd == pytest.approx(10 * (0.49 + 0.48))
    assert s.yield_ == pytest.approx(s.est_daily_usd / s.collateral_usd)


def test_score_maker_fee_rule():
    assert score(cand(pool=1.0, maker_fee=True), Book(yes=YES, no=NO), CFG) is None
    assert score(cand(pool=100.0, maker_fee=True), Book(yes=YES, no=NO), CFG) is not None


def scored(ticker, event, y):
    c = Candidate(ticker=ticker, event_ticker=event, series_ticker=event.split("-")[0],
                  daily_pool_usd=1, target_size=1000, discount=0.5, mid=0.5,
                  close_ts=NOW, maker_fee=False, price_ranges=CENT)
    return Scored(cand=c, share_yes=0, share_no=0, est_daily_usd=0, collateral_usd=1, yield_=y)


def test_choose_is_sticky_and_caps_events():
    pool = [scored("LOW", "E9-1", 0.001)]
    pool += [scored(f"A{i}", "EA-1", 1.0 - i / 100) for i in range(5)]   # one busy event
    pool += [scored(f"B{i}", f"EB-{i}", 0.5 - i / 100) for i in range(10)]
    chosen = choose(pool, current=["LOW", "GONE"], max_markets=6, max_per_event=2)
    assert chosen[0] == "LOW" and "GONE" not in chosen
    assert sum(t.startswith("A") for t in chosen) == 2
    assert len(chosen) == 6
    assert chosen[1:3] == ["A0", "A1"]
