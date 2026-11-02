import pytest

from kxmm.book import Book
from kxmm.fills import on_book, on_trade, place
from kxmm.replay import Trade

BOOK = Book(yes=[(0.50, 100), (0.49, 150)], no=[(0.49, 100), (0.48, 150)])


def t(yes_price, count, side):
    return Trade(ts=0.0, yes_price=yes_price, count=count, taker_side=side)


def test_through_trade_fills_up_to_count():
    r = place("yes", 0.49, 10, BOOK)
    assert on_trade(r, t(0.47, 4, "no")) == pytest.approx(4)
    assert r.size == pytest.approx(6)


def test_at_price_waits_for_queue():
    r = place("yes", 0.49, 10, BOOK)              # 150 displayed ahead of us
    assert r.queue_ahead == 150
    assert on_trade(r, t(0.49, 100, "no")) == 0
    assert r.queue_ahead == pytest.approx(50)
    assert on_trade(r, t(0.49, 60, "no")) == pytest.approx(10)


def test_queue_shrinks_with_displayed_size():
    r = place("yes", 0.49, 10, BOOK)
    on_book(r, Book(yes=[(0.50, 100), (0.49, 40)], no=BOOK.no))
    assert r.queue_ahead == 40
    on_book(r, Book(yes=[(0.50, 100), (0.49, 90)], no=BOOK.no))   # never grows back
    assert r.queue_ahead == 40


def test_better_level_trade_does_not_fill():
    r = place("yes", 0.49, 10, BOOK)
    assert on_trade(r, t(0.50, 50, "no")) == 0


def test_fractional_no_taker_fills_yes_bid():
    r = place("yes", 0.49, 20, BOOK)
    assert on_trade(r, t(0.48, 15.37, "no")) == pytest.approx(15.37)
    assert on_trade(r, t(0.48, 9, "yes")) == 0    # a YES taker never hits YES bids


def test_yes_taker_hits_no_bid():
    r = place("no", 0.48, 10, BOOK)               # queue 150 at NO 0.48
    assert on_trade(r, t(0.53, 7, "yes")) == pytest.approx(7)    # NO level 0.47: through
    assert on_trade(r, t(0.52, 200, "yes")) == pytest.approx(3)  # at price: 150 ahead, 50 left
