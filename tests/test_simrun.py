import shutil
import sqlite3
from pathlib import Path

from kxmm import cli, db
from kxmm.config import load_config
from kxmm.simrun import run, settlements
from tests.helpers import CENT, iso, raw_book

CFG = load_config()
T = 1_800_000_000
YES_OK = [(0.50, 60), (0.49, 60)]
NO_OK = [(0.49, 60), (0.48, 60)]


def recorder_db(tmp_path) -> Path:
    path = tmp_path / "out" / "kxmm.db"
    con = db.connect(path)
    for tk in ("A", "B"):
        db.upsert_markets(con, [{"ticker": tk, "event_ticker": f"E{tk}-1", "status": "active",
                                 "close_time": iso(T + 10 * 86400), "price_ranges": CENT}],
                          {f"E{tk}": {"fee_type": "quadratic"}}, T)
        db.upsert_programs(con, [{"id": f"p{tk}", "market_ticker": tk,
                                  "start_date": iso(T - 86400), "end_date": iso(T + 30 * 86400),
                                  "period_reward": 86.4 * 31 * 10_000, "target_size_fp": "100.00",
                                  "discount_factor_bps": 5000}], T)
        db.store_book(con, tk, T, raw_book(YES_OK, NO_OK))
        db.store_trades(con, [{"trade_id": f"{tk}1", "ticker": tk, "created_time": iso(T + 20),
                               "yes_price_dollars": "0.4900", "count_fp": "5.00",
                               "taker_side": "no"}])
        db.mark_poll(con, tk, T, 10**9)
        db.mark_poll(con, tk, T + 3600, 10**9)
    con.close()
    return path


class FakeClient:
    def __init__(self, results):
        self.results, self.calls = results, []

    def market(self, ticker):
        self.calls.append(ticker)
        if ticker not in self.results:
            raise RuntimeError("unavailable")
        return {"ticker": ticker, "result": self.results[ticker]}


def test_run_end_to_end_report(tmp_path):
    out = tmp_path / "out"
    text = run(CFG, recorder_db(tmp_path), out, None, None, client=FakeClient({"A": "yes", "B": ""}))
    assert "Strategy version 1.0.0" in text
    assert "GATE A: NOT YET" in text and "1 of 10 days" in text
    # every markout horizon and settlement, not just the 600 s primary
    assert "Markouts" in text and "60 s" in text and "3600 s" in text and "settlement" in text
    assert "Unknown 600 s markouts: 0 of" in text
    # Gate A's market count shown against what it rests on (rewards are estimates)
    assert "Positive markets:" in text and "with at least one fill" in text
    assert "market-hours per day" in text and "rewards are estimates" in text.lower()
    rows = (out / "sim_market_days.csv").read_text(encoding="utf-8").strip().splitlines()
    assert rows[0].startswith("ticker,day") and len(rows) >= 2
    con = sqlite3.connect(out / "sim.db")
    assert con.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_settlement_known_and_unknown(tmp_path):
    con = sqlite3.connect(tmp_path / "sim.db")
    c = FakeClient({"A": "yes", "B": ""})
    assert settlements(c, ["A", "B", "C"], con) == {"A": 1.0, "B": None, "C": None}
    assert settlements(None, ["A", "B"], con) == {"A": 1.0, "B": None}   # cache only
    assert settlements(c, ["A"], con) == {"A": 1.0} and c.calls.count("A") == 1


def test_cli_sim_offline(tmp_path, capsys):
    shutil.copy(Path("config.yaml"), tmp_path / "config.yaml")
    db_path = recorder_db(tmp_path)
    assert cli.main(["sim", "--db", str(db_path), "--offline"], root=tmp_path) == 0
    assert "GATE A" in capsys.readouterr().out
