import pytest

from kxmm.config import load_config
from kxmm.risk import Pnl, event_room, loss_state, reconcile

TC = load_config()["trade"]


def test_trade_config_values():
    assert TC["base_url"] == "https://external-api.demo.kalshi.co/trade-api/v2"
    assert TC["stake_usd"] == 300 and TC["quote_size"] == 1
    assert (TC["daily_loss_frac"], TC["weekly_loss_frac"], TC["max_event_loss_frac"]) == (
        0.03, 0.08, 0.10)
    assert TC["stale_book_s"] == 20 and TC["reconcile_s"] == 60


def test_pnl_round_trip():
    p = Pnl()
    p.apply_fill("K", "bid", 0.40, 10, 0.01)
    p.apply_fill("K", "ask", 0.45, 10, 0.01)
    assert p.position("K") == pytest.approx(0)
    assert p.equity({}) == pytest.approx(0.48)
    p.apply_fill("K", "bid", 0.50, 4, 0.0)
    assert p.equity({"K": 0.60}) == pytest.approx(0.48 - 2.0 + 4 * 0.60)


def test_loss_state_thresholds():
    assert loss_state(-8.99, 0.0, 0.0, TC) == "ok"
    assert loss_state(-9.00, 0.0, 0.0, TC) == "pause_day"         # 3% of $300
    assert loss_state(-9.00, 0.0, 15.0, TC) == "halt"             # -24 on the week = 8%


def test_event_room_counts_all_legs():
    room = event_room({"A": 20, "B": 20, "X": 50}, {"A": 0.5, "B": 0.5, "X": 0.5},
                      {"A": "E", "B": "E", "X": "F"}, "A", TC)
    assert room == pytest.approx(30 - 10 - 10)                    # X is another event


def test_reconcile_reports_each_mismatch():
    lines = reconcile({"A": 1, "B": 2}, {"A": 1.004, "B": 3}, {"o1"}, {"o1", "o2"})
    assert len(lines) == 2
    assert any("B" in l and "position" in l for l in lines)
    assert any("o2" in l for l in lines)
    assert reconcile({"A": 0}, {}, set(), set()) == []            # flat == absent
