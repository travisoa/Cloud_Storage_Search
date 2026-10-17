#!/usr/bin/env python3
"""Headless entry point used by the conversational agent."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from media_helper.headless_cli import main

if __name__ == "__main__":
    raise SystemExit(main())
