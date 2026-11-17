"""Invoice preview from verified rows of a committed synthetic snapshot."""
from __future__ import annotations
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from .query_rows import read_snapshot_rows

def preview_invoices(store, content_root: Path, snapshot_id: str, schemas: dict[str,str]):
    result = read_snapshot_rows(store, content_root, snapshot_id, schemas, minimum_amount=Decimal("-999999999"))
    totals = defaultdict(Decimal)
    sources = defaultdict(set)
    for row in result["rows"]:
        totals[row["order_id"]] += Decimal(row["amount"])
        sources[row["order_id"]].add(row["source"])
    return {
        "snapshot_id": snapshot_id,
        "invoices": [
            {"order_id": order_id, "amount": str(totals[order_id]),
             "source_files": sorted(sources[order_id])}
            for order_id in sorted(totals)
        ],
    }
