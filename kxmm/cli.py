"""kxmm control: start | stop | status | log | test | run  (Windows and macOS).

No PIDs or signals: the DB heartbeat says whether the loop is alive, and
`out/KILL` asks it to stop (Windows can't SIGTERM a console process). The loop
deletes KILL when it exits, which is how `stop` knows it finished.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import db
from .config import load_config
from .health import loop_state, report, stale_after_s

STOP_WAIT_S = 120
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
_sleep = time.sleep


def spawn_kwargs(platform: str) -> dict:
    if platform == "win32":
        return {"creationflags": DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def spawn_command(platform: str, executable: str, log_path: Path) -> list[str]:
    """The background loop's command line. On Windows prefer pythonw.exe: it
    has no console window that someone could close and kill the recorder with."""
    exe = executable
    if platform == "win32":
        w = Path(executable).with_name("pythonw.exe")
        if w.exists():
            exe = str(w)
    return [exe, "-u", "-m", "kxmm.loop", "--log-file", str(log_path)]


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def cmd_start(root: Path, cfg: dict) -> None:
    con = db.connect(root / cfg["output"]["db_path"])
    state, age = loop_state(con, time.time(), stale_after_s(cfg))
    if state == "RUNNING":
        print(f"already running (heartbeat {age:.0f}s ago)")
        return
    (root / "out" / "KILL").unlink(missing_ok=True)      # a stale KILL would stop it at once
    log_path = root / cfg["output"]["log_path"]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(spawn_command(sys.platform, sys.executable, log_path), cwd=root,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, env={**os.environ, "PYTHONUTF8": "1"},
                     **spawn_kwargs(sys.platform))
    print(f"recorder started; logs: {log_path} (status shows RUNNING within ~2 min)")


def cmd_stop(root: Path, cfg: dict) -> None:
    kill = root / "out" / "KILL"
    kill.parent.mkdir(parents=True, exist_ok=True)
    kill.write_text("stop\n", encoding="utf-8")
    deadline = time.monotonic() + STOP_WAIT_S
    while time.monotonic() < deadline:
        if not kill.exists():
            print("stopped")
            return
        _sleep(min(1.0, STOP_WAIT_S))
    # leave KILL: a busy loop stops after its current request; start clears it
    print("still stopping: the loop exits after its current request; run status")


def cmd_status(root: Path, cfg: dict) -> None:
    con = db.connect(root / cfg["output"]["db_path"])
    now = time.time()
    state, age = loop_state(con, now, stale_after_s(cfg))
    print(f"loop: {state}" + (f" (heartbeat {age:.0f}s ago)" if age is not None else ""))
    r = report(con, now, gap_threshold_s=cfg["record"]["gap_threshold_s"])
    print(f"universe: {r['universe']} markets | last 24h: {r['books_window']} book changes, "
          f"{r['trades_window']} trades")
    print(f"clean markets (24h, no gap > {cfg['record']['gap_threshold_s']}s): "
          f"{len(r['clean_markets'])} | longest gap: {r['max_gap_s']:.0f}s")
    for g in r["loop_gaps"]:
        print(f"  loop down {_when(g['start_ts'])} -> {_when(g['end_ts'])}")
    print("PHASE 1: " + ("PASS" if r["phase1_pass"] else "NOT YET"))


def cmd_log(root: Path, cfg: dict) -> None:
    p = root / cfg["output"]["log_path"]
    if not p.exists():
        print("no log yet")
        return
    print("".join(p.read_text(encoding="utf-8", errors="replace").splitlines(True)[-40:]),
          end="")


def main(argv: list[str] | None = None, root: Path | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    root = Path(root or Path(__file__).resolve().parent.parent)
    cmd = argv[0] if argv else ""
    if cmd == "test":
        return subprocess.call([sys.executable, "-m", "pytest", "-q"], cwd=root)
    if cmd == "run":
        from .loop import main as loop_main
        loop_main(["--config", str(root / "config.yaml")])
        return 0
    handlers = {"start": cmd_start, "stop": cmd_stop, "status": cmd_status, "log": cmd_log}
    if cmd not in handlers:
        print("usage: kxmm start|stop|status|log|test|run")
        return 2
    handlers[cmd](root, load_config(root / "config.yaml"))
    return 0
