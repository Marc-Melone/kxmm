import json

from kxmm import db
from kxmm.config import load_config
from kxmm.health import loop_state, report, stale_after_s

NOW = 1_800_000_000.0
DAY = 86400


def seeded(tmp_path, n=20):
    con = db.connect(tmp_path / "t.db")
    tickers = [f"T{i}" for i in range(n)]
    con.executemany("INSERT INTO poll_state(ticker, first_ok, last_ok) VALUES(?,?,?)",
                    [(t, NOW - DAY - 60, NOW) for t in tickers])
    con.execute("INSERT INTO selections(ts, tickers, detail) VALUES(?,?,?)",
                (NOW - 100, json.dumps(tickers), "[]"))
    con.commit()
    db.kv_set(con, "heartbeat", repr(NOW))
    return con


def test_report_phase1_pass(tmp_path):
    r = report(seeded(tmp_path), NOW)
    assert r["universe"] == 20 and len(r["clean_markets"]) == 20
    assert r["phase1_pass"] is True


def test_report_gap_breaks_pass(tmp_path):
    con = seeded(tmp_path)
    con.execute("INSERT INTO gaps(ticker, start_ts, end_ts) VALUES('T3', ?, ?)",
                (NOW - 5000, NOW - 4600))
    con.commit()
    r = report(con, NOW)
    assert len(r["clean_markets"]) == 19 and r["max_gap_s"] == 400
    assert r["phase1_pass"] is False


def test_report_loop_gap_breaks_pass(tmp_path):
    con = seeded(tmp_path, n=25)
    con.execute("INSERT INTO gaps(ticker, start_ts, end_ts) VALUES('*', ?, ?)",
                (NOW - 9000, NOW - 8000))
    con.commit()
    r = report(con, NOW)
    assert len(r["loop_gaps"]) == 1
    assert r["phase1_pass"] is False


def test_status_stale_heartbeat_is_stopped(tmp_path):
    con = db.connect(tmp_path / "t.db")
    db.kv_set(con, "heartbeat", repr(NOW - 100))
    assert loop_state(con, NOW, 30)[0] == "STOPPED"
    db.kv_set(con, "heartbeat", repr(NOW - 5))
    assert loop_state(con, NOW, 30) == ("RUNNING", 5.0)
    assert loop_state(db.connect(tmp_path / "empty.db"), NOW, 30) == ("STOPPED", None)


def test_status_after_clean_stop_is_stopped(tmp_path):
    con = db.connect(tmp_path / "t.db")
    db.kv_set(con, "heartbeat", repr(NOW - 5))
    db.kv_set(con, "stopped", repr(NOW - 4))          # loop exited cleanly after its last beat
    assert loop_state(con, NOW, 117)[0] == "STOPPED"
    db.kv_set(con, "heartbeat", repr(NOW - 1))        # restarted since
    assert loop_state(con, NOW, 117)[0] == "RUNNING"


def test_stale_after_covers_slowest_request():
    # 4 attempts x 20 s timeout + 1+2+4 s backoff between them = 87 s for one
    # request, plus three 10 s book polls
    assert stale_after_s(load_config("config.yaml")) == 117
