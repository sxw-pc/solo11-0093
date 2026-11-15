#!/usr/bin/env python
"""Standalone wrapper around the lakecommit line-count budget checker."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lakecommit.linecount import collect, format_report  # noqa: E402


def main() -> int:
    report = collect(ROOT)
    print(format_report(report))
    return 0 if report["budget_ok"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
