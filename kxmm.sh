#!/usr/bin/env bash
# kxmm control for macOS/Linux: ./kxmm.sh start|stop|status|log|test|run
DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$DIR/.venv/bin/python" "$DIR/kxmm.py" "$@"
