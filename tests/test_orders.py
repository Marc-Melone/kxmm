from kxmm.orders import LiveOrder, plan_actions, to_book
from kxmm.quoting import Quotes
from tests.helpers import CENT

Q = Quotes(yes_bid=0.49, yes_size=10, no_bid=0.48, no_size=10)


def live(side, price, remaining=10.0, filled=0.0, oid=None):
    return LiveOrder(order_id=oid or f"o-{side}", side=side, price=price,
                     remaining=remaining, filled=filled)


def test_to_book_maps_no_bids_to_yes_asks():
    assert to_book("no", 0.48) == ("ask", 0.52)
    assert to_book("yes", 0.49) == ("bid", 0.49)


def test_place_when_nothing_live():
    acts = plan_actions(Q, {}, CENT)
    assert sorted((a.kind, a.side, a.price, a.size) for a in acts) == [
        ("place", "no", 0.48, 10), ("place", "yes", 0.49, 10)]


def test_cancel_when_quote_pulled():
    acts = plan_actions(Quotes(None, 0, 0.48, 10), {"yes": live("yes", 0.49),
                                                    "no": live("no", 0.48)}, CENT)
    assert [(a.kind, a.side, a.order_id) for a in acts] == [("cancel", "yes", "o-yes")]
    assert [(a.kind, a.side) for a in plan_actions(None, {"no": live("no", 0.48)}, CENT)] == [
        ("cancel", "no")]


def test_no_action_when_unchanged():
    assert plan_actions(Q, {"yes": live("yes", 0.49), "no": live("no", 0.48)}, CENT) == []


def test_amend_on_one_tick_move():
    acts = plan_actions(Q, {"yes": live("yes", 0.50), "no": live("no", 0.48)}, CENT)
    assert [(a.kind, a.side, a.price, a.amend_count) for a in acts] == [("amend", "yes", 0.49, 10)]


def test_amend_count_includes_filled():
    # 6 already filled, 4 resting; the target is 10 resting -> total fillable 16
    acts = plan_actions(Q, {"yes": live("yes", 0.49, remaining=4, filled=6),
                            "no": live("no", 0.48)}, CENT)
    assert [(a.kind, a.side, a.amend_count) for a in acts] == [("amend", "yes", 16)]
