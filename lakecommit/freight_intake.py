"""Snapshot-bound co-delivery freight settlement from verified lake orders."""
from __future__ import annotations

import argparse
import hashlib
import json
from decimal import Decimal
from pathlib import Path

from lakecommit.fingerprint import pretty_json
from lakecommit.freight_contracts import (
    ContractRejectedError,
    enforce_contract,
    marginal_share_bills,
    value_share_bills,
)
from lakecommit.freight_routes import FreightRouteError, RouteNetwork
from lakecommit.models import FileEntry, Snapshot
from lakecommit.query_rows import read_snapshot_rows
from lakecommit.store import SnapshotStore

CENT = "0.01"


class FreightSettlementError(ValueError):
    """Raised when no invoice may be issued for the requested binding."""


def solo_quotes(data: dict, verified_rows: list[dict]) -> list[dict]:
    by_order = {r["order_id"]: r for r in verified_rows}
    result = []
    for merchant in data["merchants"]:
        order = by_order[merchant["order_id"]]
        stop = merchant["stop"]
        cost = data["distance"][data["depot"]][stop] + data["distance"][stop][data["depot"]]
        result.append({"merchant_id": merchant["merchant_id"], "order_id": merchant["order_id"],
                       "amount": order["amount"], "solo_cost": cost})
    return result


def verified_solo_quotes(store, content_root: Path, snapshot_id: str, schemas: dict, data: dict) -> list[dict]:
    rows = read_snapshot_rows(store, content_root, snapshot_id, schemas,
                              minimum_amount=Decimal("-999999999"))["rows"]
    return solo_quotes(data, rows)


def _verified_orders(store, content_root: Path, snapshot_id: str, schemas: dict, data: dict) -> dict:
    read = read_snapshot_rows(store, content_root, snapshot_id, schemas,
                              minimum_amount=Decimal("-999999999"))
    by_order = {row["order_id"]: row for row in read["rows"]}
    if len(by_order) != len(read["rows"]):
        raise FreightSettlementError("verified snapshot contains duplicate order_id rows")
    orders = []
    for merchant in data["merchants"]:
        order_id = merchant["order_id"]
        if order_id not in by_order:
            raise FreightSettlementError(f"order not present in committed snapshot: {order_id}")
        row = by_order[order_id]
        orders.append({
            "merchant_id": merchant["merchant_id"],
            "order_id": order_id,
            "amount": row["amount"],
            "source": row["source"],
        })
    return {"snapshot_id": read["snapshot_id"], "files_read": read["files_read"], "orders": orders}


def _file_identities(store, snapshot_id: str, files_read: list[str]) -> list[dict]:
    snapshot = store.load_snapshot(snapshot_id)
    by_path = {entry.path: entry for entry in snapshot.files}
    identities = []
    for path in files_read:
        entry: FileEntry = by_path[path]
        identities.append({
            "path": entry.path,
            "size_bytes": entry.size_bytes,
            "row_count": entry.row_count,
            "content_hash": entry.content_hash,
        })
    return identities


def _coalition_evidence(network: RouteNetwork, solutions: dict) -> list[dict]:
    evidence = []
    for coalition in sorted(solutions, key=lambda group: (len(group), group)):
        solution = solutions[coalition]
        evidence.append({
            "coalition": list(coalition),
            "cost": solution["cost"],
            "trips": [
                {"members": list(trip["members"]), "stops": list(trip["stops"]), "cost": trip["cost"]}
                for trip in solution["trips"]
            ],
        })
    return evidence


def build_settlement(data: dict, verified: dict, network: RouteNetwork) -> dict:
    """Compute fleet optima, both contracts, and all bound/conservation checks."""
    merchants = network.merchant_ids
    orders = {item["merchant_id"]: item for item in verified["orders"]}
    order_amounts = {mid: orders[mid]["amount"] for mid in merchants}
    solutions = network.all_coalition_costs()
    coalition_costs = {coalition: solution["cost"] for coalition, solution in solutions.items()}
    grand = solutions[merchants]
    solo = {mid: solutions[(mid,)]["cost"] for mid in merchants}
    total_cents = grand["cost"] * 100
    solo_upper_cents = {mid: solo[mid] * 100 for mid in merchants}
    contracts = _build_contracts(merchants, order_amounts, coalition_costs, total_cents, solo_upper_cents)
    return {
        "snapshot_id": verified["snapshot_id"],
        "file_identities": _evidence_identities(verified),
        "merchants": [_merchant_record(network, orders, mid, solo) for mid in merchants],
        "solo_total_cost": sum(solo.values()),
        "grand_fleet": {
            "cost": grand["cost"],
            "trips": [
                {"members": list(trip["members"]), "stops": list(trip["stops"]),
                 "units": network.trip_units(trip["members"]), "capacity": network.capacity,
                 "cost": trip["cost"]}
                for trip in grand["trips"]
            ],
        },
        "coalition_optima": _coalition_evidence(network, solutions),
        "contracts": contracts,
    }


def _build_contracts(merchants, order_amounts, coalition_costs, total_cents, solo_upper_cents) -> dict:
    value = value_share_bills(merchants, order_amounts, total_cents)
    marginal = marginal_share_bills(merchants, coalition_costs, total_cents)
    contracts = {}
    for name, detail in (("value_share", value), ("marginal_share", marginal)):
        record = enforce_contract(name, merchants, detail["bills_cents"], solo_upper_cents, total_cents)
        record.update({key: value_ for key, value_ in detail.items() if key != "bills_cents"})
        record["invoices"] = {
            mid: {"cents": detail["bills_cents"][mid], "amount": _cents_text(detail["bills_cents"][mid])}
            for mid in merchants
        }
        contracts[name] = record
    return contracts


def _merchant_record(network: RouteNetwork, orders: dict, mid: str, solo: dict) -> dict:
    merchant = next(item for item in network.merchants if item["merchant_id"] == mid)
    order = orders[mid]
    return {
        "merchant_id": mid,
        "stop": merchant["stop"],
        "units": network.units_by_merchant[mid],
        "order_id": merchant["order_id"],
        "order_amount": order["amount"],
        "source_file": order["source"],
        "solo_cost": solo[mid],
        "solo_upper_cents": solo[mid] * 100,
    }


def _evidence_identities(verified: dict) -> list[dict]:
    return [dict(identity) for identity in verified["identities"]]


def _cents_text(cents: int) -> str:
    return str((Decimal(cents) / Decimal(100)).quantize(Decimal(CENT)))


def settle(store, content_root: Path, snapshot_id: str, schemas: dict, data: dict) -> dict:
    """Full snapshot-bound settlement; raises before issuing any invoice."""
    try:
        network = RouteNetwork(data)
    except FreightRouteError as exc:
        raise FreightSettlementError(str(exc)) from exc
    verified = _verified_orders(store, content_root, snapshot_id, schemas, data)
    verified["identities"] = _file_identities(store, snapshot_id, verified["files_read"])
    try:
        settlement = build_settlement(data, verified, network)
    except (ContractRejectedError, FreightRouteError) as exc:
        raise FreightSettlementError(str(exc)) from exc
    settlement["files_read"] = verified["files_read"]
    return settlement


def build_ephemeral_store(workdir: Path, content_root: Path, snapshot_id: str,
                         files: list[dict], schemas: dict) -> tuple[SnapshotStore, dict]:
    """Commit query-root fixture files into an isolated, deterministic store."""
    entries = []
    resolved_schemas = {}
    for item in sorted(files, key=lambda value: value["path"]):
        path = item["path"]
        content = (content_root / path).read_bytes()
        entries.append(FileEntry(path=path, partition=dict(item.get("partition", {})),
                                 size_bytes=len(content), row_count=len([
                                     line for line in content.decode("utf-8").splitlines() if line.strip()]),
                                 content_hash=hashlib.sha256(content).hexdigest()))
        resolved_schemas[path] = schemas[path]
    store = SnapshotStore(workdir)
    if store.current_path.exists():
        store.reset()
    store.initialize(Snapshot(snapshot_id=snapshot_id, parent_snapshot_id=None, sequence=0,
                              logical_time="2025-01-01T00:00:00Z", files=tuple(entries)), ("region",))
    return store, resolved_schemas


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Snapshot-bound co-delivery freight settlement.")
    parser.add_argument("--fixture", type=Path, default=Path("fixtures/lake_freight/intake.json"))
    parser.add_argument("--content-root", type=Path, default=Path("fixtures/query_rows"))
    parser.add_argument("--snapshot-id", default="query-root")
    parser.add_argument("--workdir", type=Path, default=Path(".lakecommit-freight"))
    args = parser.parse_args(argv)
    data = json.loads(args.fixture.read_text(encoding="utf-8"))
    root = args.content_root.resolve()
    files = [
        {"path": "data/region=us/v1.jsonl", "partition": {"region": "us"}},
        {"path": "data/region=us/v2.jsonl", "partition": {"region": "us"}},
    ]
    schemas = {"data/region=us/v1.jsonl": "cents-v1", "data/region=us/v2.jsonl": "dollars-v2"}
    store, resolved = build_ephemeral_store(args.workdir, root, args.snapshot_id, files, schemas)
    try:
        settlement = settle(store, root, args.snapshot_id, resolved, data)
    except FreightSettlementError as exc:
        print(pretty_json({"error": {"code": "FREIGHT_SETTLEMENT_REJECTED", "message": str(exc)}}))
        return 3
    print(pretty_json(settlement))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
