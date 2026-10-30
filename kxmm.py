"""kxmm control: python kxmm.py start|stop|status|log|test|run"""
import sys

from kxmm.cli import main

if __name__ == "__main__":
    sys.exit(main())
