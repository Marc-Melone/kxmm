import math

import pytest

from kxmm.book import Book
from kxmm.fair import EwmaVol, expit, logit, microprice


def test_microprice_leans_to_thin_side():
    # YES bid 0.49 x 100; NO bid 0.49 x 300 -> YES ask 0.51 with 300 behind it
    b = Book(yes=[(0.49, 100)], no=[(0.49, 300)])
    assert microprice(b) == pytest.approx(0.49 * 300 / 400 + 0.51 * 100 / 400)


def test_microprice_none_on_one_side():
    assert microprice(Book(yes=[(0.49, 100)], no=[])) is None


def test_logit_expit_roundtrip():
    for p in (0.05, 0.3, 0.5, 0.9):
        assert expit(logit(p)) == pytest.approx(p)
    assert math.isfinite(logit(0.0)) and math.isfinite(logit(1.0))


def test_ewma_converges_to_constant_rate():
    v = EwmaVol(halflife_s=3600)
    assert v.var_per_s is None
    for i in range(500):
        v.update(i * 60.0, 0.1 if i % 2 else -0.1)
    assert v.var_per_s == pytest.approx(0.04 / 60, rel=0.01)   # each step moves 0.2
