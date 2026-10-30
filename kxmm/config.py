"""Load config.yaml and fail loudly on a missing knob."""
from __future__ import annotations

from pathlib import Path

import yaml

REQUIRED = (
    "api.base_url", "api.rate_per_sec",
    "record.book_poll_s", "record.trades_poll_s", "record.universe_refresh_s",
    "record.max_markets", "record.gap_threshold_s",
    "output.db_path", "output.log_path",
)


def load_config(path: str | Path = "config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    for dotted in REQUIRED:
        node = cfg
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                raise ValueError(f"config is missing required key {dotted}")
            node = node[part]
    return cfg
