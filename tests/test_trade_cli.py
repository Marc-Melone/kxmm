import shutil
from pathlib import Path

from kxmm import cli, tradedb
from kxmm.config import load_config
from kxmm.drills import DRILLS
from kxmm.gateb import gate_b
from tests.fakes import FakeTradeClient

TC = load_config()["trade"]
D0 = 1_800_000_000 - 1_800_000_000 % 86400            # a UTC midnight


def setup_root(tmp_path, monkeypatch):
    shutil.copy(Path("config.yaml"), tmp_path / "config.yaml")
    monkeypatch.delenv("KALSHI_DEMO_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_DEMO_KEY_PATH", raising=False)
    return tmp_path


def test_start_without_keys_explains(tmp_path, monkeypatch, capsys):
    root = setup_root(tmp_path, monkeypatch)
    assert cli.main(["trade", "start"], root=root) == 1
    out = capsys.readouterr().out
    assert "KALSHI_DEMO_KEY_ID" in out and "README" in out


def test_panic_cancels_without_loop(tmp_path, monkeypatch, capsys):
    root = setup_root(tmp_path, monkeypatch)
    fake = FakeTradeClient()
    fake.orders["o1"] = {"order_id": "o1", "ticker": "K", "book_side": "bid", "price": 0.03,
                         "remaining": 1.0, "filled": 0.0, "group": None}
    cli.cmd_trade_panic(root, load_config(root / "config.yaml"), client=fake)
    assert fake.orders == {}
    assert (root / "out" / "trade" / "HALT").exists() and (root / "out" / "trade" / "KILL").exists()


def full_days(con, first_day, n):
    for d in range(first_day, first_day + n):
        for minute in range(24 * 60):
            tradedb.heartbeat(con, D0 + d * 86400 + minute * 60)


def test_gate_b_needs_clean_days_and_drills(tmp_path):
    con = tradedb.connect(tmp_path / "trade.db")
    full_days(con, 0, 4)
    now = D0 + 4 * 86400 + 600                         # 10 minutes into day 5
    ok, reasons = gate_b(con, now, TC)
    assert not ok and any("4 of 5 clean days" in r for r in reasons)
    full_days(con, 4, 1)
    now = D0 + 5 * 86400 + 600
    ok, reasons = gate_b(con, now, TC)
    assert not ok and any("drills" in r for r in reasons)
    for name in DRILLS:
        tradedb.drill_result(con, D0 + 2 * 86400, name, True, "ok")
    assert gate_b(con, now, TC) == (True, [])
    tradedb.event(con, D0 + 2 * 86400 + 5, "error", "boom")     # day 3 is no longer clean
    ok, reasons = gate_b(con, now, TC)
    assert not ok and any("2 of 5 clean days" in r for r in reasons)
