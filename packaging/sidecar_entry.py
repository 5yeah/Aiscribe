"""PyInstaller entry point for the Aiscribe stdio sidecar.

The Node plugin starts this binary with `--stdio`; running it without arguments
also defaults to stdio so a manually launched executable cannot accidentally
open an HTTP port silently.
"""

from __future__ import annotations

import multiprocessing
import sys

from ai_report.mcp_server import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    argv = sys.argv[1:] or ["--stdio"]
    raise SystemExit(main(argv))
