import json
from pathlib import Path

import pytest

from kxmm.book import Book, book_digest

FIX = Path(__file__).parent / "fixtures"


def test_book_from_fixture():
    raw = json.loads((FIX / "orderbook.json").read_text(encoding="utf-8"))["orderbook_fp"]
    b = Book.from_orderbook(raw)
    assert b.best_yes_bid == pytest.approx(0.95)
    assert b.best_no_bid == pytest.approx(0.01)
    assert b.yes_ask == pytest.approx(0.99)
    assert b.mid() == pytest.approx(0.97, abs=1e-9)
    assert b.spread() == pytest.approx(0.04, abs=1e-9)
    assert b.yes[0] == (pytest.approx(0.95), pytest.approx(44.0))   # best first


def test_book_handles_null_and_empty_sides():
    b = Book.from_orderbook({"yes_dollars": None, "no_dollars": []})
    assert b.yes == [] and b.no == []
    assert b.mid() is None and b.spread() is None and b.yes_ask is None
    assert Book.from_orderbook({}).mid() is None


def test_digest_changes_with_size():
    a = {"yes_dollars": [["0.5000", "10.00"]], "no_dollars": [["0.4800", "5.00"]]}
    b = {"yes_dollars": [["0.5000", "11.00"]], "no_dollars": [["0.4800", "5.00"]]}
    assert book_digest(a) != book_digest(b)
    assert book_digest(a) == book_digest(json.loads(json.dumps(a)))
