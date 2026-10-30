import logging
import sys

from kxmm.loop import configure_logging


def test_log_file_is_utf8_and_catches_stderr(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stderr", sys.stderr)          # restored after the test
    log = tmp_path / "out" / "kxmm.log"
    configure_logging(log)
    logging.getLogger("kxmm.test").info("market — 50¢ mid")
    print("traceback text", file=sys.stderr)
    for h in logging.getLogger().handlers:
        h.flush()
    sys.stderr.flush()
    text = log.read_text(encoding="utf-8")
    assert "market — 50¢ mid" in text and "traceback text" in text
    logging.getLogger().handlers.clear()
