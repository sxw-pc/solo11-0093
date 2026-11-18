"""Sellable rights catalog and fictional license bid validation."""

from __future__ import annotations

from dataclasses import dataclass

from lakecommit import config
from lakecommit.errors import SchemaError
from lakecommit.fingerprint import object_fingerprint
from lakecommit.models import FileEntry, Snapshot, TableSpec
from lakecommit.snapshot import manifest_hash as compute_manifest_hash

MAX_BIDS = 8
MAX_REGIONS = 4
ROW_WEIGHTED = "row_weighted"
STANDALONE_FLOOR = "standalone_floor"
CONTRACT_RULES = {
    ROW_WEIGHTED: "partition_row_share",
    STANDALONE_FLOOR: "owner_outside_option_then_equal_surplus",
}


def _fail(code: str, message: str, field: str = "", details: dict | None = None) -> None:
    raise SchemaError(code, message, field_path=field, details=details)


@dataclass(frozen=True)
class Bid:
    bid_id: str
    licensee: str
    regions: tuple[str, ...]
    value_cents: int

    @property
    def value(self) -> int:
        return self.value_cents // 100

    def to_dict(self) -> dict:
        return {
            "bid_id": self.bid_id,
            "licensee": self.licensee,
            "regions": list(self.regions),
            "value": self.value,
            "value_cents": self.value_cents,
        }


@dataclass(frozen=True)
class RightsCatalog:
    """Rights derived only from one committed snapshot's file identities."""

    snapshot_id: str
    table: str
    sequence: int
    logical_time: str
    files: tuple[FileEntry, ...]
    regions: tuple[str, ...]
    row_counts: dict[str, int]
    size_bytes: dict[str, int]
    manifest_hash: str

    @classmethod
    def from_snapshot(cls, snapshot: Snapshot, table: TableSpec) -> "RightsCatalog":
        if "region" not in table.partition_columns:
            _fail("PARTITION_COLUMN_REQUIRED", "license sales require a region partition column")
        files = tuple(sorted(snapshot.files, key=lambda entry: entry.path))
        if not files:
            _fail("SNAPSHOT_HAS_NO_FILES", "a license sale needs committed files", details={"snapshot_id": snapshot.snapshot_id})
        rows: dict[str, int] = {}
        sizes: dict[str, int] = {}
        for entry in files:
            region = entry.partition.get("region")
            if not region:
                _fail("PARTITION_REGION_MISSING", "every committed file must declare a region", details={"path": entry.path})
            rows[region] = rows.get(region, 0) + entry.row_count
            sizes[region] = sizes.get(region, 0) + entry.size_bytes
        if len(rows) > MAX_REGIONS:
            _fail("REGION_LIMIT_EXCEEDED", f"at most {MAX_REGIONS} regions may be licensed", details={"regions": sorted(rows)})
        digest = snapshot.manifest_hash or compute_manifest_hash(files)
        return cls(
            snapshot_id=snapshot.snapshot_id,
            table=table.table_name,
            sequence=snapshot.sequence,
            logical_time=snapshot.logical_time,
            files=files,
            regions=tuple(sorted(rows)),
            row_counts={region: rows[region] for region in sorted(rows)},
            size_bytes={region: sizes[region] for region in sorted(rows)},
            manifest_hash=digest,
        )

    def identity(self) -> dict:
        region_rows = []
        for region in self.regions:
            region_rows.append({
                "region": region,
                "row_count": self.row_counts[region],
                "size_bytes": self.size_bytes[region],
                "files": [entry.to_dict() for entry in self.files if entry.partition.get("region") == region],
            })
        body = {
            "snapshot_id": self.snapshot_id,
            "table": self.table,
            "sequence": self.sequence,
            "logical_time": self.logical_time,
            "manifest_hash": self.manifest_hash,
            "file_count": len(self.files),
            "regions": region_rows,
        }
        return {**body, "fingerprint": object_fingerprint(body)}


@dataclass(frozen=True)
class LicenseIntake:
    regions: tuple[str, ...]
    owners: dict[str, str]
    bids: tuple[Bid, ...]
    contracts: dict[str, str]


def _identifier(value: object, code: str, field: str) -> str:
    if not isinstance(value, str) or not config.OBJECT_ID_RE.match(value):
        _fail(code, "expected a stable identifier", field)
    return value


def _parse_bid(raw: object, index: int, regions: tuple[str, ...]) -> Bid:
    path = f"bids[{index}]"
    if not isinstance(raw, dict):
        _fail("BID_INVALID", "each bid must be an object", path)
    bid_id = _identifier(raw.get("bid_id"), "BID_ID_INVALID", f"{path}.bid_id")
    licensee = _identifier(raw.get("licensee"), "BID_LICENSEE_INVALID", f"{path}.licensee")
    raw_regions = raw.get("regions")
    if not isinstance(raw_regions, list) or not raw_regions:
        _fail("BID_REGIONS_INVALID", "a bid must request at least one region", f"{path}.regions")
    if len(raw_regions) > MAX_REGIONS:
        _fail("BID_REGION_LIMIT", f"a bid may request at most {MAX_REGIONS} regions", f"{path}.regions")
    requested = []
    for position, region in enumerate(raw_regions):
        if not isinstance(region, str) or region not in regions:
            _fail("BID_REGION_UNKNOWN", "bid requests a region absent from the snapshot", f"{path}.regions[{position}]", {"region": region})
        if region in requested:
            _fail("BID_REGION_DUPLICATE", "a bid may not repeat a region", f"{path}.regions[{position}]")
        requested.append(region)
    value = raw.get("value")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail("BID_VALUE_INVALID", "bid value must be a non-negative integer", f"{path}.value")
    return Bid(bid_id=bid_id, licensee=licensee, regions=tuple(sorted(requested)), value_cents=value * 100)


def parse_intake(data: object, catalog: RightsCatalog) -> LicenseIntake:
    if not isinstance(data, dict):
        _fail("INTAKE_INVALID", "license intake must be an object", "$")
    if data.get("snapshot_id") != catalog.snapshot_id:
        _fail("INTAKE_SNAPSHOT_MISMATCH", "intake does not target this snapshot", "snapshot_id", {"snapshot_id": data.get("snapshot_id")})
    if data.get("table") != catalog.table:
        _fail("INTAKE_TABLE_MISMATCH", "intake table does not match the snapshot table", "table")
    raw_regions = data.get("regions")
    if (not isinstance(raw_regions, list) or not raw_regions
            or any(not isinstance(region, str) for region in raw_regions)
            or len(set(raw_regions)) != len(raw_regions)):
        _fail("INTAKE_REGIONS_INVALID", "regions must be a non-empty unique string array", "regions")
    declared = tuple(sorted(raw_regions))
    if set(declared) != set(catalog.regions):
        _fail("INTAKE_REGIONS_MISMATCH", "declared regions must match committed snapshot regions", "regions", {
            "missing_from_intake": sorted(set(catalog.regions) - set(declared)),
            "not_in_snapshot": sorted(set(declared) - set(catalog.regions)),
        })
    raw_owners = data.get("owners")
    if not isinstance(raw_owners, dict) or set(raw_owners) != set(declared):
        _fail("INTAKE_OWNERS_INVALID", "owners must name exactly one owner per region", "owners")
    owners = {}
    for region in declared:
        owner = raw_owners[region]
        if not isinstance(owner, str) or not owner:
            _fail("INTAKE_OWNERS_INVALID", "owner names must be non-empty strings", f"owners.{region}")
        owners[region] = owner
    raw_bids = data.get("bids")
    if not isinstance(raw_bids, list):
        _fail("INTAKE_BIDS_INVALID", "bids must be an array", "bids")
    if len(raw_bids) > MAX_BIDS:
        _fail("BID_LIMIT_EXCEEDED", f"at most {MAX_BIDS} bids may be enumerated", "bids", {"count": len(raw_bids)})
    bids = [_parse_bid(item, index, declared) for index, item in enumerate(raw_bids)]
    ids = [bid.bid_id for bid in bids]
    duplicates = sorted({bid_id for bid_id in ids if ids.count(bid_id) > 1})
    if duplicates:
        _fail("BID_ID_DUPLICATE", "bid IDs must be unique", "bids", {"bid_ids": duplicates})
    offers = [(bid.licensee, bid.regions) for bid in bids]
    repeated = sorted(
        (
            {"licensee": license_, "regions": list(regions)}
            for license_, regions in set(offers) if offers.count((license_, regions)) > 1
        ),
        key=lambda item: (item["licensee"], item["regions"]),
    )
    if repeated:
        _fail("LICENSEE_OFFER_DUPLICATE", "a licensee repeated an identical region offer", "bids",
              {"offers": repeated})
    raw_contracts = data.get("contracts")
    if not isinstance(raw_contracts, dict) or not raw_contracts:
        _fail("INTAKE_CONTRACTS_INVALID", "contracts must be a non-empty object", "contracts")
    contracts = {}
    for name, document in raw_contracts.items():
        if name not in CONTRACT_RULES:
            _fail("CONTRACT_UNKNOWN", "unknown royalty contract", f"contracts.{name}")
        if not isinstance(document, dict) or document.get("royalty_rule") != CONTRACT_RULES[name]:
            _fail("CONTRACT_RULE_INVALID", "contract royalty rule does not match its name", f"contracts.{name}.royalty_rule")
        contracts[name] = CONTRACT_RULES[name]
    return LicenseIntake(
        regions=declared,
        owners={region: owners[region] for region in sorted(owners)},
        bids=tuple(sorted(bids, key=lambda bid: bid.bid_id)),
        contracts={name: contracts[name] for name in sorted(contracts)},
    )
