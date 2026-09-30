"""Vercel entrypoint.

Vercel's Python runtime looks for an ASGI app named `app` in this file.
Everything real lives in src/vingo_scraper/api/main.py -- this is only the
adapter so the same code serves locally and on Vercel.
"""

from __future__ import annotations

import sys
from pathlib import Path

# On Vercel the repo root is the working directory, and src/ is not installed.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vingo_scraper.api.main import app  # noqa: E402

__all__ = ["app"]
