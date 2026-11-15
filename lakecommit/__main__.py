"""Allow ``python -m lakecommit``."""

from __future__ import annotations

import sys

from lakecommit.cli import main

if __name__ == "__main__":
    sys.exit(main())
