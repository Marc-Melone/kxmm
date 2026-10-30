"""Entry point: python -m kxmm.loop [--config config.yaml]"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import db
from .config import load_config
from .kalshi_public import KalshiPublic
from .recorder import Recorder


FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_logging(log_file: Path | None) -> None:
    """Foreground: log to stdout. Background (`start`): log straight to a UTF-8
    file and send stderr there too, so uncaught tracebacks are kept even under
    pythonw.exe, which has no console."""
    if log_file is None:
        logging.basicConfig(level=logging.INFO, stream=sys.stdout, format=FORMAT)
        return
    log_file = Path(log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    sys.stderr = open(log_file, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    handler = logging.StreamHandler(sys.stderr)        # one stream: lines stay in order
    handler.setFormatter(logging.Formatter(FORMAT))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="kxmm Phase 1 recorder loop")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--log-file", help="log here (UTF-8) instead of stdout")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    root = Path(args.config).resolve().parent
    configure_logging(Path(args.log_file) if args.log_file else None)
    api = cfg["api"]
    client = KalshiPublic(api["base_url"], api["rate_per_sec"], api.get("timeout_s", 20),
                          api.get("max_attempts", 4))
    con = db.connect(root / cfg["output"]["db_path"])
    logging.getLogger("kxmm").info("recorder starting (pid-free; stop with out/KILL)")
    Recorder(cfg, client, con, root).run()


if __name__ == "__main__":
    main()
