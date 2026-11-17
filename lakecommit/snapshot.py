"""Pure helpers for immutable snapshot manifests and deterministic snapshot IDs."""

from __future__ import annotations

from lakecommit.fingerprint import object_fingerprint
from lakecommit.models import FileEntry, Snapshot, partition_label


def normalize_files(files: list[FileEntry] | tuple[FileEntry, ...]) -> tuple[FileEntry, ...]:
    return tuple(sorted(files, key=lambda entry: entry.path))


def file_payload(files: list[FileEntry] | tuple[FileEntry, ...]) -> list[dict]:
    return [entry.to_dict() for entry in normalize_files(files)]


def manifest_hash(files: list[FileEntry] | tuple[FileEntry, ...]) -> str:
    return object_fingerprint(file_payload(files))


def commit_hash(commit: dict) -> str:
    return object_fingerprint(commit)


def next_snapshot_id(
    parent_snapshot_id: str,
    sequence: int,
    attempt_id: str,
    payload_hash: str,
    logical_time: str,
) -> str:
    seed = {
        "parent_snapshot_id": parent_snapshot_id,
        "sequence": sequence,
        "attempt_id": attempt_id,
        "payload_hash": payload_hash,
        "logical_time": logical_time,
    }
    digest = object_fingerprint(seed)[:12]
    return f"snap-{sequence:04d}-{digest}"


def build_commit(
    *,
    snapshot_id: str,
    parent_snapshot_id: str,
    sequence: int,
    logical_time: str,
    attempt_id: str,
    writer_id: str,
    operation_type: str,
    base_snapshot_id: str,
    payload_hash: str,
    touched_partitions: list[str],
    added_paths: list[str],
    removed_paths: list[str],
    rebase: dict | None,
) -> dict:
    return {
        "snapshot_id": snapshot_id,
        "parent_snapshot_id": parent_snapshot_id,
        "sequence": sequence,
        "logical_time": logical_time,
        "attempt_id": attempt_id,
        "writer_id": writer_id,
        "operation_type": operation_type,
        "base_snapshot_id": base_snapshot_id,
        "payload_hash": payload_hash,
        "touched_partitions": sorted(set(touched_partitions)),
        "added_paths": sorted(set(added_paths)),
        "removed_paths": sorted(set(removed_paths)),
        "rebase": rebase,
    }


def build_manifest(
    *,
    snapshot_id: str,
    parent_snapshot_id: str | None,
    sequence: int,
    logical_time: str,
    files: list[FileEntry] | tuple[FileEntry, ...],
    commit: dict,
) -> dict:
    ordered = normalize_files(files)
    return {
        "snapshot_id": snapshot_id,
        "parent_snapshot_id": parent_snapshot_id,
        "sequence": sequence,
        "logical_time": logical_time,
        "file_count": len(ordered),
        "manifest_hash": manifest_hash(ordered),
        "commit_hash": commit_hash(commit),
        "files": [entry.to_dict() for entry in ordered],
    }


def build_initial_commit(
    snapshot_id: str,
    logical_time: str,
    files: tuple[FileEntry, ...],
    partition_columns: tuple[str, ...],
) -> dict:
    return {
        "snapshot_id": snapshot_id,
        "parent_snapshot_id": None,
        "sequence": 0,
        "logical_time": logical_time,
        "attempt_id": None,
        "writer_id": None,
        "operation_type": "bootstrap",
        "base_snapshot_id": None,
        "payload_hash": None,
        "touched_partitions": sorted(
            {partition_label(entry.partition, partition_columns) for entry in files}
        ),
        "added_paths": sorted(entry.path for entry in files),
        "removed_paths": [],
        "rebase": None,
    }


def snapshot_from_manifest(manifest: dict, files: tuple[FileEntry, ...]) -> Snapshot:
    return Snapshot(
        snapshot_id=str(manifest["snapshot_id"]),
        parent_snapshot_id=manifest.get("parent_snapshot_id"),
        sequence=int(manifest["sequence"]),
        logical_time=str(manifest["logical_time"]),
        files=normalize_files(files),
        manifest_hash=str(manifest.get("manifest_hash", "")),
    )
