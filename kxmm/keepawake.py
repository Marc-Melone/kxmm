"""Keep the host awake while the loop runs.

Windows: SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) while
inside the block, ES_CONTINUOUS alone on exit. macOS: `caffeinate -i -w <pid>`,
which also dies with this process. Anything else: no-op. The power plan should
still be set to never sleep (README) — this only covers the running loop.
"""
from __future__ import annotations

import contextlib
import os
import subprocess
import sys

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


@contextlib.contextmanager
def keep_awake(platform: str | None = None, kernel32=None, popen=subprocess.Popen):
    platform = platform or sys.platform
    if platform == "win32":
        if kernel32 is None:
            import ctypes
            kernel32 = ctypes.windll.kernel32
        kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        try:
            yield
        finally:
            kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    elif platform == "darwin":
        proc = popen(["caffeinate", "-i", "-w", str(os.getpid())])
        try:
            yield
        finally:
            proc.terminate()
    else:
        yield
