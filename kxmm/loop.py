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


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="kxmm Phase 1 recorder loop")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    root = Path(args.config).resolve().parent
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    api = cfg["api"]
    client = KalshiPublic(api["base_url"], api["rate_per_sec"], api.get("timeout_s", 20),
                          api.get("max_attempts", 4))
    con = db.connect(root / cfg["output"]["db_path"])
    logging.getLogger("kxmm").info("recorder starting (pid-free; stop with out/KILL)")
    Recorder(cfg, client, con, root).run()


if __name__ == "__main__":
    main()
