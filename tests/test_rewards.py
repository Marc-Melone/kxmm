import pytest

from kxmm.rewards import daily_pool_usd, est_share, side_score, tick_size

CENT = [{"start": "0.0000", "end": "1.0000", "step": "0.0100"}]
SUBPENNY = [{"start": "0.0000", "end": "0.1000", "step": "0.0010"},
            {"start": "0.1000", "end": "0.9000", "step": "0.0100"},
            {"start": "0.9000", "end": "1.0000", "step": "0.0010"}]
LEVELS = [(0.50, 100), (0.49, 150), (0.48, 300), (0.47, 1000)]


def test_side_score_reference_and_discount():
    s = side_score(LEVELS, target_size=1000, discount=0.5, price_ranges=CENT)
    assert s.ref_price == pytest.approx(0.49)
    assert s.qualifying_score == pytest.approx(650.0)
    assert s.depth == pytest.approx(1550)
    assert s.meets_target


def test_est_share_of_ten():
    s = side_score(LEVELS, target_size=1000, discount=0.5, price_ranges=CENT)
    assert est_share(s, 10) == pytest.approx(10 / 660)


def test_thin_side_scores_nothing():
    s = side_score([(0.30, 400), (0.29, 500)], target_size=1000, discount=0.5,
                   price_ranges=CENT)
    assert s.meets_target is False
    assert est_share(s, 10) == 0.0


def test_tick_size_subpenny():
    assert tick_size(0.05, SUBPENNY) == pytest.approx(0.001)
    assert tick_size(0.5, SUBPENNY) == pytest.approx(0.01)
    assert tick_size(1.0, SUBPENNY) == pytest.approx(0.001)


def test_daily_pool_usd():
    p = {"period_reward": 1_000_000, "start_date": "2026-10-30T00:00:00Z",
         "end_date": "2026-11-06T00:00:00Z"}
    assert daily_pool_usd(p) == pytest.approx(100 / 7)
