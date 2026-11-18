"""Intake validation and committed-snapshot identity for resilience planning."""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

from lakecommit import config
from lakecommit.errors import SchemaError
from lakecommit.fingerprint import object_fingerprint
from lakecommit.loader import load_table
from lakecommit.snapshot import manifest_hash
from lakecommit.store import SnapshotStore

MAX_NODES = 8
MAX_REGIONS = 5
MAX_SCENARIOS = 4
MAX_HARDENED_NODES = 2
DEFAULT_MAX_HARDENED_NODES = 1
MAX_REPAIRED_NODES = 1
CONTRACT_NAMES = ("continuous_region", "eu_repair_window")
DEFAULT_TABLE = "tables/lake_sales_orders.json"
NODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,31}$")
SCENARIO_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,63}$")


def _fail(code: str, message: str, field: str, details: dict | None = None) -> None:
    raise SchemaError(code, message, field_path=field, details=details or {})


def _cost(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail("COST_INVALID", "costs must be integers", field)
    if value < 0:
        _fail("COST_NEGATIVE", "costs may not be negative", field)
    return value


def _node(value: object, field: str) -> str:
    if not isinstance(value, str) or not NODE_RE.match(value):
        _fail("NODE_UNKNOWN", "node ids must be short stable identifiers", field)
    return value


def _resolve_table(value: object, base_dir: Path | None) -> Path:
    if not isinstance(value, str) or not value.strip() or "://" in value:
        _fail("SNAPSHOT_UNVERIFIED", "a local table fixture is required", "table")
    relative = Path(value.strip())
    if relative.is_absolute():
        return relative
    candidates = []
    if base_dir is not None:
        candidates.append(Path(base_dir) / relative)
    candidates.extend([config.FIXTURES_DIR / relative,
                       config.FIXTURES_DIR / "storage_resilience" / relative,
                       config.ROOT / relative, Path.cwd() / relative])
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def _committed(store: SnapshotStore, snapshot_id: str):
    if not store.has_snapshot(snapshot_id):
        _fail("SNAPSHOT_NOT_COMMITTED", "snapshot is not committed in the store",
              "snapshot_id", {"snapshot_id": snapshot_id})
    snapshot = store.load_snapshot(snapshot_id)
    commit = store.load_commit(snapshot_id)
    if snapshot.manifest_hash and snapshot.manifest_hash != manifest_hash(snapshot.files):
        _fail("SNAPSHOT_INTEGRITY_ERROR", "manifest hash does not match committed files",
              "snapshot_id", {"snapshot_id": snapshot_id})
    if commit.get("snapshot_id") != snapshot_id:
        _fail("SNAPSHOT_INTEGRITY_ERROR", "commit does not describe the snapshot",
              "snapshot_id", {"snapshot_id": snapshot_id})
    return snapshot, commit


def _load_snapshot(data: dict, store: SnapshotStore | None, base_dir: Path | None):
    snapshot_id = data.get("snapshot_id")
    if not isinstance(snapshot_id, str) or not config.OBJECT_ID_RE.match(snapshot_id):
        _fail("SNAPSHOT_ID_INVALID", "snapshot_id must be a stable identifier", "snapshot_id")
    if store is not None:
        return _committed(store, snapshot_id)
    table_path = _resolve_table(data.get("table") or DEFAULT_TABLE, base_dir)
    table, initial = load_table(table_path)
    if initial.snapshot_id != snapshot_id:
        _fail("SNAPSHOT_NOT_COMMITTED", "intake snapshot is not the committed table snapshot",
              "snapshot_id", {"expected": initial.snapshot_id, "actual": snapshot_id})
    with tempfile.TemporaryDirectory(prefix="lakecommit-resilience-") as temporary:
        ephemeral = SnapshotStore(Path(temporary))
        ephemeral.initialize(initial, table.partition_columns)
        return _committed(ephemeral, snapshot_id)


def _region_of(entry) -> str:
    region = entry.partition.get("region")
    if region is None and len(entry.partition) == 1:
        region = next(iter(entry.partition.values()))
    if not isinstance(region, str) or not region:
        _fail("SNAPSHOT_REGION_INVALID", "each committed file needs a region identity",
              "snapshot.files", {"path": entry.path})
    return region


def _identity(snapshot, commit: dict) -> dict:
    if not snapshot.files:
        _fail("SNAPSHOT_FILES_EMPTY", "snapshot has no files", "snapshot_id")
    paths = [entry.path for entry in snapshot.files]
    if len(paths) != len(set(paths)):
        _fail("SNAPSHOT_INTEGRITY_ERROR", "snapshot contains duplicate file paths", "snapshot.files")
    files, region_files, region_rows = [], {}, {}
    for entry in snapshot.files:
        region = _region_of(entry)
        files.append({
            "path": entry.path,
            "region": region,
            "partition": dict(entry.partition),
            "size_bytes": entry.size_bytes,
            "row_count": entry.row_count,
            "content_hash": entry.content_hash,
        })
        region_files.setdefault(region, []).append(entry.path)
        region_rows[region] = region_rows.get(region, 0) + entry.row_count
    return {
        "snapshot_id": snapshot.snapshot_id,
        "parent_snapshot_id": snapshot.parent_snapshot_id,
        "sequence": snapshot.sequence,
        "logical_time": snapshot.logical_time,
        "manifest_hash": manifest_hash(snapshot.files),
        "commit_hash": object_fingerprint(commit),
        "file_count": len(files),
        "files": files,
        "regions": sorted(region_files),
        "region_files": {region: sorted(values) for region, values in sorted(region_files.items())},
        "region_row_counts": {region: region_rows[region] for region in sorted(region_rows)},
    }


def _validate_hardening_bound(data: dict) -> int:
    raw = data.get("max_hardened_nodes", DEFAULT_MAX_HARDENED_NODES)
    if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= MAX_HARDENED_NODES:
        _fail("HARDENING_BOUND_INVALID",
              "max_hardened_nodes must be an integer between 1 and 2", "max_hardened_nodes")
    return raw


def _validate_costs(data: dict) -> tuple[list[str], dict]:
    harden, repair = data.get("harden_cost"), data.get("repair_cost")
    if not isinstance(harden, dict) or not isinstance(repair, dict) or not harden:
        _fail("COST_TABLE_REQUIRED", "harden_cost and repair_cost must be non-empty objects",
              "harden_cost")
    if set(harden) != set(repair):
        _fail("COST_TABLE_MISMATCH", "harden and repair costs must cover the same nodes",
              "repair_cost")
    if len(harden) > MAX_NODES:
        _fail("SCALE_LIMIT_EXCEEDED", "at most 8 nodes are supported", "harden_cost")
    nodes = sorted(harden)
    costs = {node: {"harden": _cost(harden[node], f"harden_cost.{node}"),
                    "repair": _cost(repair[node], f"repair_cost.{node}")} for node in nodes}
    return nodes, costs


def _validate_replicas(data: dict, nodes: list[str], regions: list[str]) -> dict:
    replicas = data.get("region_replicas")
    if not isinstance(replicas, dict):
        _fail("REGION_REPLICAS_REQUIRED", "region_replicas must be an object", "region_replicas")
    if set(replicas) != set(regions):
        missing, unknown = sorted(set(regions) - set(replicas)), sorted(set(replicas) - set(regions))
        code = "REGION_COVERAGE_MISSING" if missing else "REGION_UNKNOWN"
        _fail(code, "region_replicas must match committed snapshot regions",
              "region_replicas", {"missing": missing, "unknown": unknown})
    coverage = {}
    for region in sorted(regions):
        raw = replicas[region]
        if not isinstance(raw, list) or not raw:
            _fail("REGION_REPLICA_MISSING", "each region needs at least one replica node",
                  f"region_replicas.{region}")
        values = [_node(item, f"region_replicas.{region}") for item in raw]
        if len(values) != len(set(values)):
            _fail("REGION_REPLICA_DUPLICATE", "replica nodes must be unique",
                  f"region_replicas.{region}")
        unknown = sorted(set(values) - set(nodes))
        if unknown:
            _fail("NODE_UNKNOWN", "replica lists reference unknown nodes",
                  f"region_replicas.{region}", {"nodes": unknown})
        coverage[region] = sorted(values)
    return coverage


def _validate_scenarios(data: dict, nodes: list[str]) -> list[dict]:
    scenarios = data.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        _fail("SCENARIO_REQUIRED", "at least one scenario is required", "scenarios")
    if len(scenarios) > MAX_SCENARIOS:
        _fail("SCALE_LIMIT_EXCEEDED", "at most 4 scenarios are supported", "scenarios")
    seen, normalized = set(), []
    for index, raw in enumerate(scenarios):
        if not isinstance(raw, dict):
            _fail("SCENARIO_INVALID", "scenarios must be objects", f"scenarios[{index}]")
        scenario_id = raw.get("scenario_id")
        if not isinstance(scenario_id, str) or not SCENARIO_RE.match(scenario_id):
            _fail("SCENARIO_ID_INVALID", "scenario_id must be a stable identifier",
                  f"scenarios[{index}].scenario_id")
        if scenario_id in seen:
            _fail("SCENARIO_ID_DUPLICATE", "scenario ids must be unique",
                  f"scenarios[{index}].scenario_id", {"scenario_id": scenario_id})
        seen.add(scenario_id)
        failed = raw.get("failed_nodes")
        if not isinstance(failed, list):
            _fail("FAILED_NODES_REQUIRED", "failed_nodes must be an array",
                  f"scenarios[{index}].failed_nodes")
        failed_nodes = [_node(item, f"scenarios[{index}].failed_nodes") for item in failed]
        if len(failed_nodes) != len(set(failed_nodes)):
            _fail("FAILED_NODES_DUPLICATE", "failed_nodes must be unique",
                  f"scenarios[{index}].failed_nodes")
        unknown = sorted(set(failed_nodes) - set(nodes))
        if unknown:
            _fail("NODE_UNKNOWN", "failed_nodes reference unknown nodes",
                  f"scenarios[{index}].failed_nodes", {"nodes": unknown})
        normalized.append({"scenario_id": scenario_id, "failed_nodes": sorted(failed_nodes)})
    return sorted(normalized, key=lambda item: item["scenario_id"])


def _validate_contracts(data: dict, regions: list[str]) -> dict:
    contracts = data.get("contracts")
    if not isinstance(contracts, dict) or not contracts:
        _fail("CONTRACT_REQUIRED", "at least one authorized contract is required", "contracts")
    normalized = {}
    for name in sorted(contracts):
        if name not in CONTRACT_NAMES:
            _fail("CONTRACT_UNKNOWN", "unknown service contract", f"contracts.{name}")
        terms = contracts[name]
        if not isinstance(terms, dict):
            _fail("CONTRACT_INVALID", "contract terms must be an object", f"contracts.{name}")
        allowed = terms.get("allowed_initial_outage_regions")
        if not isinstance(allowed, list) or any(not isinstance(item, str) for item in allowed):
            _fail("CONTRACT_INVALID", "allowed_initial_outage_regions must be an array",
                  f"contracts.{name}.allowed_initial_outage_regions")
        if len(allowed) != len(set(allowed)):
            _fail("CONTRACT_INVALID", "allowed regions must be unique",
                  f"contracts.{name}.allowed_initial_outage_regions")
        unknown = sorted(set(allowed) - set(regions))
        if unknown:
            _fail("CONTRACT_REGION_UNKNOWN", "contract references an unknown region",
                  f"contracts.{name}.allowed_initial_outage_regions", {"regions": unknown})
        compensation = _cost(terms.get("outage_compensation"),
                             f"contracts.{name}.outage_compensation")
        if name == "continuous_region" and (allowed or compensation != 0):
            _fail("CONTRACT_INVALID", "continuous_region permits no outage and no compensation",
                  f"contracts.{name}")
        if name == "eu_repair_window" and allowed != ["eu"]:
            _fail("CONTRACT_INVALID", "eu_repair_window allows only the committed eu region",
                  f"contracts.{name}")
        normalized[name] = {"allowed_initial_outage_regions": sorted(allowed),
                            "outage_compensation": compensation}
    return normalized


def load_intake(data: dict, *, store: SnapshotStore | None = None,
                base_dir: Path | None = None) -> tuple[dict, dict]:
    """Validate intake and resolve one committed snapshot identity."""
    if not isinstance(data, dict):
        _fail("INTAKE_INVALID", "intake must be an object", "$")
    snapshot, commit = _load_snapshot(data, store, base_dir)
    identity = _identity(snapshot, commit)
    if len(identity["regions"]) > MAX_REGIONS:
        _fail("SCALE_LIMIT_EXCEEDED", "at most 5 regions are supported", "region_replicas")
    nodes, costs = _validate_costs(data)
    replicas = _validate_replicas(data, nodes, identity["regions"])
    scenarios = _validate_scenarios(data, nodes)
    contracts = _validate_contracts(data, identity["regions"])
    spec = {"nodes": nodes, "costs": costs, "region_replicas": replicas,
            "scenarios": scenarios, "contracts": contracts,
            "max_hardened_nodes": _validate_hardening_bound(data)}
    return identity, spec
