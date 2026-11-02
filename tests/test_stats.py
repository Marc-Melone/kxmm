import pytest

from kxmm.config import load_config
from kxmm.sim import Fill, MarketResult
from kxmm.stats import (FillStats, MarketDay, bootstrap_lower, day_totals, fill_stats,
                        gate_a, market_days, t_stat)

S = load_config()["sim"]
DAY = "2027-01-15"
TS = 1_800_000_000                                   # inside DAY (UTC)


def fill(markout, fee=0.0, price=0.5, size=10):
    return Fill("K", TS, "yes", price, size, fee, markouts={600: markout})


def result(fills, reward=0.0, ticker="K"):
    return MarketResult(ticker, fills, {DAY: reward}, {DAY: 100.0}, 0.0, 0.0, 0.5)


def test_market_days_attribution():
    mds = market_days([result([fill(0.30, 0.01), fill(-0.10, 0.01)], reward=1.0)], S)
    assert len(mds) == 1
    md = mds[0]
    assert md.trading == pytest.approx(0.18)
    assert md.reward == pytest.approx(0.50) and md.total == pytest.approx(0.68)
    assert day_totals(mds, "total") == {DAY: pytest.approx(0.68)}


def test_bootstrap_lower_is_deterministic_and_sensible():
    up = [1, 2, 3, 4, 5] * 4
    assert bootstrap_lower(up, 2000, 7) > 0
    assert bootstrap_lower(up, 2000, 7) == bootstrap_lower(up, 2000, 7)
    assert bootstrap_lower([-2, -1, 0, 1, 2] * 4, 2000, 7) <= 0
    assert t_stat([1.0, 2.0, 3.0]) == pytest.approx(2.0 / (1.0 / 3 ** 0.5))
    assert t_stat([1.0]) is None


def test_fill_stats_sample_size():
    # per-fill returns 0.1484 and -0.0756 on $5 notional: mean 0.0364, sample sd 0.1584
    res = [result([fill(0.1484 * 5), fill(-0.0756 * 5)])]
    fs = fill_stats(res, market_days(res, S), S)
    assert fs.n == 2 and fs.mean == pytest.approx(0.0364)
    assert fs.sd == pytest.approx(0.1584, abs=1e-4)
    assert fs.fills_needed == 76
    assert fs.per_market_day == pytest.approx(2.0)


def md(ticker, total, day=DAY, trading=None):
    return MarketDay(ticker, day, 1, total if trading is None else trading, 0.0, 0.0, total)


FS_OK = FillStats(n=120, mean=0.03, sd=0.15, fills_needed=100, per_market_day=2.0)


def test_gate_a_needs_days_and_fills():
    mds = [md("A", 1.0)]
    short = gate_a(mds, 9, FS_OK, 0.5, S)
    assert short.passed is False and short.verdict == "NOT YET"
    assert any("9 of 10 days" in r for r in short.reasons)
    few = gate_a(mds, 10, FillStats(80, 0.03, 0.15, 100, 2.0), 0.5, S)
    assert few.passed is False and any("80 of 100 fills" in r for r in few.reasons)


def test_gate_a_pass_rewards_only_and_chance():
    ok = gate_a([md("A", 1.0, trading=0.4)], 10, FS_OK, 0.5, S)
    assert (ok.passed, ok.verdict) == (True, "PASS")
    assert ok.live_days_needed == pytest.approx(100 / (2.0 * 5))
    rewards_only = gate_a([md("A", 1.0, trading=-0.2)], 10, FS_OK, 0.5, S)
    assert (rewards_only.passed, rewards_only.verdict) == (True, "PASS — rewards only")
    chance = gate_a([md("A", 1.0, trading=0.4)], 10, FS_OK, -0.1, S)
    assert chance.passed is False and any("beyond chance" in r for r in chance.reasons)
    assert gate_a([md("A", 1.0)], 10, FS_OK, None, S).passed is False   # under 2 days
