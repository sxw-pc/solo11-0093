"""Normalized, deterministic JSON result report."""

from __future__ import annotations

from pathlib import Path

from lakecommit import config, conflicts
from lakecommit.fingerprint import object_fingerprint, write_json_atomic
from lakecommit.models import Decision, ScenarioBundle, Snapshot
from lakecommit.store import SnapshotStore


def _snapshot_chain_rows(store: SnapshotStore) -> list[dict]:
    rows: list[dict] = []
    for snapshot in reversed(store.chain()):
        commit = store.load_commit(snapshot.snapshot_id)
        rows.append(
            {
                "snapshot_id": snapshot.snapshot_id,
                "parent_snapshot_id": snapshot.parent_snapshot_id,
                "sequence": snapshot.sequence,
                "logical_time": snapshot.logical_time,
                "file_count": len(snapshot.files),
                "manifest_hash": snapshot.manifest_hash,
                "attempt_id": commit.get("attempt_id"),
                "writer_id": commit.get("writer_id"),
                "operation_type": commit.get("operation_type"),
                "touched_partitions": commit.get("touched_partitions", []),
                "added_paths": commit.get("added_paths", []),
                "removed_paths": commit.get("removed_paths", []),
                "rebase": commit.get("rebase"),
            }
        )
    return rows


def _summary(decisions: list[Decision], chain: list[dict]) -> dict:
    accepted = sum(1 for item in decisions if item.status == "accepted")
    rejected = sum(1 for item in decisions if item.status == "rejected")
    applied = sum(1 for item in decisions if item.outcome == "applied")
    replayed = sum(1 for item in decisions if item.outcome == "replayed")
    return {
        "attempts": len(decisions),
        "accepted": accepted,
        "rejected": rejected,
        "applied": applied,
        "replayed": replayed,
        "retry_required": sum(1 for item in decisions if item.retry_required),
        "safe_rebases": sum(
            1
            for item in decisions
            if item.status == "accepted" and item.rebase is not None
        ),
        "conflict_code_counts": conflicts.conflict_counts(decisions),
        "snapshots_created": len(chain) - 1,
        "final_file_count": chain[-1]["file_count"] if chain else 0,
    }


def build_report(
    bundle: ScenarioBundle,
    decisions: list[Decision],
    store: SnapshotStore,
    verification: dict,
) -> dict:
    chain = _snapshot_chain_rows(store)
    final = store.current_snapshot()
    report = {
        "schema_version": config.SCHEMA_VERSION,
        "scenario": bundle.to_report_meta(),
        "table": {
            "table_name": bundle.table.table_name,
            "partition_columns": list(bundle.table.partition_columns),
            "file_fields": list(bundle.table.file_fields),
        },
        "protocol": dict(bundle.protocol),
        "initial_snapshot": {
            "snapshot_id": bundle.initial_snapshot.snapshot_id,
            "sequence": bundle.initial_snapshot.sequence,
            "logical_time": bundle.initial_snapshot.logical_time,
            "file_count": len(bundle.initial_snapshot.files),
        },
        "attempts": [decision.to_dict() for decision in decisions],
        "snapshot_chain": chain,
        "final_snapshot": {
            "snapshot_id": final.snapshot_id,
            "parent_snapshot_id": final.parent_snapshot_id,
            "sequence": final.sequence,
            "logical_time": final.logical_time,
            "file_count": len(final.files),
            "manifest_hash": final.manifest_hash,
            "files": [entry.to_dict() for entry in final.files],
        },
        "summary": _summary(decisions, chain),
        "state_verification": verification,
    }
    report["run_fingerprint"] = object_fingerprint(report)
    return report


def write_json_report(workdir: str | Path, report: dict) -> Path:
    path = Path(workdir) / config.REPORT_JSON
    write_json_atomic(path, report)
    return path


def load_report(workdir: str | Path) -> dict:
    import json

    path = Path(workdir) / config.REPORT_JSON
    return json.loads(path.read_text(encoding="utf-8"))
