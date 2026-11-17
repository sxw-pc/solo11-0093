"""Bounded row reader for synthetic files named by a committed snapshot."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

from lakecommit.store import SnapshotStore


def read_snapshot_rows(
    store: SnapshotStore,
    content_root: Path,
    snapshot_id: str,
    schemas: dict[str, str],
    *,
    minimum_amount: Decimal,
) -> dict:
    """Read all verified JSONL files and filter normalized business amounts.

    The reader intentionally has no statistics or pruning index. It is a simple
    correctness baseline for consumers of committed snapshot file identities.
    """
    snapshot = store.load_snapshot(snapshot_id)
    root = content_root.resolve()
    rows = []
    files_read = []
    for entry in sorted(snapshot.files, key=lambda item: item.path):
        file_path = (root / entry.path).resolve()
        if not file_path.is_relative_to(root) or not file_path.is_file():
            raise ValueError(f"missing or unsafe content file: {entry.path}")
        content = file_path.read_bytes()
        if len(content) != entry.size_bytes or hashlib.sha256(content).hexdigest() != entry.content_hash:
            raise ValueError(f"content does not match committed file: {entry.path}")
        schema = schemas.get(entry.path)
        if schema not in {"cents-v1", "dollars-v2"}:
            raise ValueError(f"unknown schema for committed file: {entry.path}")
        file_rows = [json.loads(line) for line in content.decode("utf-8").splitlines() if line.strip()]
        if len(file_rows) != entry.row_count:
            raise ValueError(f"row count does not match committed file: {entry.path}")
        files_read.append(entry.path)
        for row in file_rows:
            if schema == "cents-v1":
                amount = Decimal(str(row["amount_cents"])) / Decimal(100)
            else:
                amount = Decimal(str(row["amount_dollars"]))
            if amount >= minimum_amount:
                rows.append({"order_id": str(row["order_id"]), "amount": str(amount), "source": entry.path})
    rows.sort(key=lambda row: (row["order_id"], row["source"]))
    return {"snapshot_id": snapshot.snapshot_id, "files_read": files_read, "rows": rows}
