import pytest

from kxmm.book import Book
from kxmm.config import load_config
from kxmm.quoting import make_quotes
from kxmm.replay import Meta, Program
from tests.helpers import CENT, NO, NOW, YES

S = load_config()["sim"]
META = Meta("K", CENT, close_ts=NOW + 10 * 86400, occurrence_ts=None, maker_fee=False)
PROG = Program(start=NOW - 86400, end=NOW + 7 * 86400, daily_pool=100, target=1000, discount=0.5)
BOOK = Book(yes=YES, no=NO)                       # YES ask 0.51, mid 0.505


def q(book=BOOK, position=0.0, var=1e-9, now=NOW, meta=META, program=PROG):
    return make_quotes(book, position, var, now, meta, program, S)


def test_flat_position_quotes_at_reference_when_risk_is_small():
    r = q()
    assert r.yes_bid == pytest.approx(0.49) and r.no_bid == pytest.approx(0.48)
    assert r.yes_size == 10 and r.no_size == 10


def test_long_position_skews_down_and_caps():
    r = q(position=20, var=1e-5)                  # ~0.17 log-odds skew: target ~0.45
    assert r.yes_bid is not None and r.yes_bid < 0.49
    assert r.yes_size == pytest.approx(15 / 0.505 - 20)
    capped = q(position=30, var=1e-5)             # 15 / 0.505 = 29.7 < 30
    assert capped.yes_bid is None and capped.no_bid is not None


def test_blackout_returns_none():
    assert q(now=META.close_ts - 259200 + 1) is None
    early = Meta("K", CENT, close_ts=NOW + 30 * 86400, occurrence_ts=NOW + 2 * 86400,
                 maker_fee=False)
    assert q(meta=early) is None                  # occurrence comes first


def test_no_quotes_on_crossed_or_one_sided_book():
    assert q(book=Book(yes=[(0.52, 100)], no=[(0.49, 100)])) is None   # bid 0.52 >= ask 0.51
    assert q(book=Book(yes=YES, no=[])) is None
    assert q(var=None) is None
    assert q(program=None) is None


def test_subpenny_ticks_round_down():
    ranges = [{"start": "0.0000", "end": "0.1000", "step": "0.0010"},
              {"start": "0.1000", "end": "0.9000", "step": "0.0100"},
              {"start": "0.9000", "end": "1.0000", "step": "0.0010"}]
    meta = Meta("K", ranges, close_ts=NOW + 10 * 86400, occurrence_ts=None, maker_fee=False)
    book = Book(yes=[(0.050, 100), (0.049, 150), (0.048, 300), (0.047, 1000)],
                no=[(0.945, 100), (0.944, 150), (0.943, 300), (0.942, 1000)])
    r = q(book=book, meta=meta, var=1e-5)
    for price in (r.yes_bid, r.no_bid):
        assert price is not None
        assert abs(price * 1000 - round(price * 1000)) < 1e-9
