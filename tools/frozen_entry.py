"""Entry point for the PyInstaller build (absolute imports only)."""
from __future__ import annotations

import sys

from dispatch.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
