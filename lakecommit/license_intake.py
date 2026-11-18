"""Partition-license sale intake and legacy single-region preview."""
from __future__ import annotations
import argparse
import sys
import tempfile
from pathlib import Path

from lakecommit import config, loader
from lakecommit.errors import LakeCommitError, SchemaError
from lakecommit.fingerprint import pretty_json
from lakecommit.license_market import RightsCatalog
from lakecommit.license_sale import sell
from lakecommit.store import SnapshotStore


def preview_single_region(data: dict) -> list[str]:
    free=set(data["regions"])
    winners=[]
    for bid in sorted(data["bids"],key=lambda x:(-x["value"],x["bid_id"])):
        if len(bid["regions"])!=1: continue
        region=bid["regions"][0]
        if region in free:
            free.remove(region)
            winners.append(bid["bid_id"])
    return winners


def open_initial_store(workdir: str | Path) -> tuple[object, SnapshotStore]:
    """Initialize a throwaway store from the committed table fixture, then read it back."""
    table, snapshot = loader.load_table(config.FIXTURES_DIR / "tables" / "lake_sales_orders.json")
    store = SnapshotStore(workdir)
    store.reset()
    store.initialize(snapshot, table.partition_columns)
    return table, store


def run_sale(fixture: Path) -> dict:
    data = loader.load_json(fixture)
    with tempfile.TemporaryDirectory() as temporary:
        table, store = open_initial_store(temporary)
        if not store.has_snapshot(str(data.get("snapshot_id"))):
            raise SchemaError(
                "SNAPSHOT_ID_UNKNOWN",
                "intake snapshot does not exist in the committed table fixture",
                details={"snapshot_id": data.get("snapshot_id"), "available": store.snapshot_ids()},
            )
        snapshot = store.load_snapshot(str(data["snapshot_id"]))
    catalog = RightsCatalog.from_snapshot(snapshot, table)
    return sell(catalog, data)


def main(argv=None):
    p=argparse.ArgumentParser(description="Synthetic partition-license sale intake.")
    p.add_argument("--fixture",type=Path,default=Path("fixtures/license_market/intake.json"))
    args=p.parse_args(argv)
    try:
        report = run_sale(args.fixture)
    except LakeCommitError as exc:
        sys.stderr.write(pretty_json(exc.as_dict()))
        return exc.exit_code
    sys.stdout.write(pretty_json(report))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
