"""Fictional renewable-credit retirement for verified synthetic lake reads.

Loads are metered from files committed to a named snapshot in
``lakecommit.store.SnapshotStore`` (``size_bytes / 100`` energy units, scaled
by the fixture's ``energy_units_per_100_bytes``).  Every retirement decision
is proved by an exact optimal allocation over the contract's legal
certificate->load edges (see ``lakecommit.carbon_match``), never by arrival
order: the legacy first-fit preview below is kept only as a counterexample.
All numbers are synthetic metering; no real-world carbon reduction or
regulatory eligibility is claimed.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

from lakecommit import loader
from lakecommit.carbon_match import MATCHINGS, fmt_units, solve_contract
from lakecommit.errors import InputError, LakeCommitError
from lakecommit.fingerprint import pretty_json
from lakecommit.store import SnapshotStore

# Certified solver bounds for the exact matching (flow network) approach.
MAX_LOADS = 8
MAX_CERTIFICATES = 10
MAX_REGIONS = 4
MAX_HOURS = 4

DISCLAIMER = (
    "Synthetic metering only; no real-world carbon reduction or regulatory "
    "eligibility is claimed."
)


def preview_hourly_offsets(data: dict, sizes: dict[str, int]) -> dict:
    """Legacy first-fit preview, kept as a counterexample, not as evidence."""
    remaining = {c["certificate_id"]: Decimal(c["units"]) for c in data["certificates"]}
    uncovered = {}
    for load in data["loads"]:
        needed = Decimal(sizes[load["file_path"]]) / Decimal(100)
        for credit in data["certificates"]:
            if credit["region"] != load["region"] or load["hour"] not in credit["eligible_hours"]:
                continue
            use = min(needed, remaining[credit["certificate_id"]])
            needed -= use
            remaining[credit["certificate_id"]] -= use
        uncovered[load["load_id"]] = str(needed)
    return {"uncovered": uncovered}


def _fail(code: str, message: str, **details) -> None:
    raise InputError(code, message, details={k: v for k, v in details.items() if v is not None} or None)


def _parse_decimal(value, *, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        _fail("CARBON_UNITS_INVALID", f"{field} must be a decimal number", field=field)
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        _fail("CARBON_UNITS_INVALID", f"{field} is not a valid decimal", field=field)
    if not result.is_finite():
        _fail("CARBON_UNITS_INVALID", f"{field} must be finite", field=field)
    return result


def _parse_id(value, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        _fail("CARBON_INPUT_INVALID", f"{field} must be a non-empty string", field=field)
    return value


def _parse_hour(value, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail("CARBON_INPUT_INVALID", f"{field} must be an integer hour", field=field)
    return value


def _validate_loads(raw_loads, manifest: dict, energy: Decimal) -> list[dict]:
    if not isinstance(raw_loads, list) or not raw_loads:
        _fail("CARBON_NO_LOADS", "intake must name at least one read load")
    loads = []
    seen = set()
    for index, item in enumerate(raw_loads):
        field = f"loads[{index}]"
        if not isinstance(item, dict):
            _fail("CARBON_INPUT_INVALID", "load must be an object", field=field)
        load_id = _parse_id(item.get("load_id"), field=f"{field}.load_id")
        if load_id in seen:
            _fail("CARBON_DUPLICATE_LOAD_ID", "duplicate load id", load_id=load_id)
        seen.add(load_id)
        file_path = _parse_id(item.get("file_path"), field=f"{field}.file_path")
        region = _parse_id(item.get("region"), field=f"{field}.region")
        hour = _parse_hour(item.get("hour"), field=f"{field}.hour")
        entry = manifest.get(file_path)
        if entry is None:
            _fail("CARBON_UNKNOWN_FILE", "load reads a file not committed in the snapshot",
                  load_id=load_id, file_path=file_path)
        committed_region = entry.partition.get("region")
        if committed_region != region:
            _fail("CARBON_PARTITION_MISMATCH", "load region differs from the committed partition",
                  load_id=load_id, file_path=file_path, region=region, committed_region=committed_region)
        if entry.size_bytes < 0:
            _fail("CARBON_NEGATIVE_LOAD", "committed file has negative size", file_path=file_path)
        units = (Decimal(entry.size_bytes) / Decimal(100)) * energy
        loads.append({"load_id": load_id, "file_path": file_path, "region": region, "hour": hour,
                      "size_bytes": entry.size_bytes, "content_hash": entry.content_hash, "units": units})
    return loads


def _validate_certificates(raw_certs, manifest_regions: set) -> list[dict]:
    if not isinstance(raw_certs, list):
        _fail("CARBON_INPUT_INVALID", "certificates must be an array", field="certificates")
    certs = []
    seen = set()
    for index, item in enumerate(raw_certs):
        field = f"certificates[{index}]"
        if not isinstance(item, dict):
            _fail("CARBON_INPUT_INVALID", "certificate must be an object", field=field)
        certificate_id = _parse_id(item.get("certificate_id"), field=f"{field}.certificate_id")
        if certificate_id in seen:
            _fail("CARBON_DUPLICATE_CERTIFICATE_ID", "duplicate certificate id", certificate_id=certificate_id)
        seen.add(certificate_id)
        region = _parse_id(item.get("region"), field=f"{field}.region")
        if region not in manifest_regions:
            _fail("CARBON_UNKNOWN_REGION", "certificate region is not part of the committed snapshot",
                  certificate_id=certificate_id, region=region)
        raw_hours = item.get("eligible_hours")
        if not isinstance(raw_hours, list):
            _fail("CARBON_INPUT_INVALID", "eligible_hours must be an array", field=f"{field}.eligible_hours")
        eligible_hours = frozenset(_parse_hour(h, field=f"{field}.eligible_hours") for h in raw_hours)
        units = _parse_decimal(item.get("units"), field=f"{field}.units")
        if units < 0:
            _fail("CARBON_NEGATIVE_UNITS", "certificate units must not be negative", certificate_id=certificate_id)
        annual_transfer = item.get("annual_transfer")
        if not isinstance(annual_transfer, bool):
            _fail("CARBON_INPUT_INVALID", "annual_transfer must be a boolean", field=f"{field}.annual_transfer")
        certs.append({"certificate_id": certificate_id, "region": region, "eligible_hours": eligible_hours,
                      "units": units, "annual_transfer": annual_transfer})
    return certs


def _validate_contracts(raw_contracts) -> dict:
    if not isinstance(raw_contracts, dict) or not raw_contracts:
        _fail("CARBON_INPUT_INVALID", "contracts must be a non-empty object", field="contracts")
    contracts = {}
    for name, spec in raw_contracts.items():
        if not isinstance(spec, dict):
            _fail("CARBON_INPUT_INVALID", "contract must be an object", field=f"contracts.{name}")
        matching = spec.get("matching")
        if matching not in MATCHINGS:
            _fail("CARBON_UNKNOWN_MATCHING", "unknown contract matching rule", contract=name, matching=matching)
        max_uncovered = _parse_decimal(spec.get("max_uncovered"), field=f"contracts.{name}.max_uncovered")
        if max_uncovered < 0:
            _fail("CARBON_NEGATIVE_THRESHOLD", "max_uncovered must not be negative", contract=name)
        contracts[str(name)] = {"matching": matching, "max_uncovered": max_uncovered}
    return contracts


def _check_bounds(loads: list[dict], certs: list[dict]) -> None:
    regions = {load["region"] for load in loads} | {cert["region"] for cert in certs}
    hours = {load["hour"] for load in loads}
    for cert in certs:
        hours |= set(cert["eligible_hours"])
    if (len(loads) > MAX_LOADS or len(certs) > MAX_CERTIFICATES
            or len(regions) > MAX_REGIONS or len(hours) > MAX_HOURS):
        _fail("CARBON_SCALE_EXCEEDED", "intake exceeds the certified solver bounds",
              loads=len(loads), certificates=len(certs), regions=len(regions), hours=len(hours))


def plan_certificates(store: SnapshotStore, data: dict) -> dict:
    """Verify intake loads against the committed snapshot and decide issuance.

    Returns per-contract retirement evidence (allocations, remainders,
    uncovered loads, issuance flag) computed from the optimal legal matching.
    Raises ``InputError`` for unknown snapshots/files, duplicate ids, negative
    units, certificate regions outside the committed manifest, empty
    snapshots, or inputs beyond the certified solver bounds.
    """
    if not isinstance(data, dict):
        _fail("CARBON_INPUT_INVALID", "intake fixture must be a JSON object")
    snapshot_id = data.get("snapshot_id")
    if not isinstance(snapshot_id, str) or not snapshot_id:
        _fail("CARBON_INPUT_INVALID", "snapshot_id must be a non-empty string", field="snapshot_id")
    if not store.has_snapshot(snapshot_id):
        _fail("CARBON_UNKNOWN_SNAPSHOT", "snapshot is not committed in the store", snapshot_id=snapshot_id)
    snapshot = store.load_snapshot(snapshot_id)
    if not snapshot.files:
        _fail("CARBON_EMPTY_SNAPSHOT", "snapshot commits no files to meter", snapshot_id=snapshot_id)
    energy = _parse_decimal(data.get("energy_units_per_100_bytes", "1"), field="energy_units_per_100_bytes")
    if energy <= 0:
        _fail("CARBON_ENERGY_RATE_INVALID", "energy_units_per_100_bytes must be positive")
    manifest = snapshot.files_by_path()
    manifest_regions = {entry.partition.get("region") for entry in snapshot.files}
    loads = _validate_loads(data.get("loads"), manifest, energy)
    certs = _validate_certificates(data.get("certificates"), manifest_regions)
    contracts = _validate_contracts(data.get("contracts"))
    _check_bounds(loads, certs)
    loads = sorted(loads, key=lambda load: load["load_id"])
    certs = sorted(certs, key=lambda cert: cert["certificate_id"])
    return {
        "snapshot_id": snapshot.snapshot_id,
        "manifest_hash": snapshot.manifest_hash,
        "synthetic": True,
        "disclaimer": DISCLAIMER,
        "energy_units_per_100_bytes": fmt_units(energy),
        "loads": [
            {"load_id": load["load_id"], "file_path": load["file_path"], "region": load["region"],
             "hour": load["hour"], "size_bytes": load["size_bytes"],
             "content_hash": load["content_hash"], "units": fmt_units(load["units"])}
            for load in loads
        ],
        "certificates": [
            {"certificate_id": cert["certificate_id"], "region": cert["region"],
             "eligible_hours": sorted(cert["eligible_hours"]),
             "annual_transfer": cert["annual_transfer"], "units": fmt_units(cert["units"])}
            for cert in certs
        ],
        "contracts": {
            name: solve_contract(loads, certs, spec["matching"], spec["max_uncovered"])
            for name, spec in sorted(contracts.items())
        },
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="lakecommit.carbon_intake",
        description="Issue synthetic renewable-credit retirements for committed snapshot reads.",
    )
    parser.add_argument("--fixture", type=Path, default=Path("fixtures/carbon/intake.json"))
    parser.add_argument("--table", type=Path, default=Path("fixtures/tables/lake_sales_orders.json"))
    parser.add_argument("--workdir", type=Path, default=None,
                        help="existing committed snapshot store; default builds an ephemeral store from --table")
    args = parser.parse_args(argv)
    try:
        data = loader.load_json(args.fixture)
        if args.workdir is not None:
            report = plan_certificates(SnapshotStore(args.workdir), data)
        else:
            table, initial = loader.load_table(args.table)
            with tempfile.TemporaryDirectory(prefix="lakecommit-carbon-") as workdir:
                store = SnapshotStore(workdir)
                store.initialize(initial, table.partition_columns)
                report = plan_certificates(store, data)
        sizes = {load["file_path"]: load["size_bytes"] for load in report["loads"]}
        report["legacy_greedy_preview"] = {
            "authoritative": False,
            "note": "first-fit preview kept as a counterexample; not a retirement decision",
            "uncovered": preview_hourly_offsets(data, sizes)["uncovered"],
        }
        sys.stdout.write(pretty_json(report))
        sys.stdout.flush()
        return 0
    except LakeCommitError as exc:
        sys.stderr.write(pretty_json(exc.as_dict()))
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
