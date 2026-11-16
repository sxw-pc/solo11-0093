"""Filesystem-backed immutable snapshot store with an atomically updated pointer."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from lakecommit import config, snapshot as snapshot_helpers
from lakecommit.errors import SchemaError, StateError
from lakecommit.fingerprint import write_json_atomic, write_text_atomic
from lakecommit.models import Attempt, FileEntry, Snapshot


class SnapshotStore:
    def __init__(self, workdir: str | Path) -> None:
        self.workdir = Path(workdir)
        self.state_dir = self.workdir / config.STATE_DIRNAME
        self.snapshots_dir = self.state_dir / config.SNAPSHOTS_DIRNAME
        self.current_path = self.state_dir / config.CURRENT_FILENAME
        self.ledger_path = self.state_dir / config.LEDGER_FILENAME
        self.journal_dir = self.state_dir / config.JOURNAL_DIRNAME

    def reset(self) -> None:
        if self.state_dir.exists():
            shutil.rmtree(self.state_dir)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self.journal_dir.mkdir(parents=True, exist_ok=True)

    def initialize(self, initial_snapshot: Snapshot, partition_columns: tuple[str, ...]) -> Snapshot:
        if self.current_path.exists():
            raise StateError("STATE_ALREADY_INITIALIZED", "snapshot store already has a current pointer")
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        if initial_snapshot.parent_snapshot_id is not None or initial_snapshot.sequence != 0:
            raise SchemaError("INITIAL_SNAPSHOT_INVALID", "initial snapshot must be sequence 0 with no parent")
        files = snapshot_helpers.normalize_files(initial_snapshot.files)
        commit = snapshot_helpers.build_initial_commit(
            initial_snapshot.snapshot_id,
            initial_snapshot.logical_time,
            files,
            partition_columns,
        )
        manifest = snapshot_helpers.build_manifest(
            snapshot_id=initial_snapshot.snapshot_id,
            parent_snapshot_id=None,
            sequence=0,
            logical_time=initial_snapshot.logical_time,
            files=files,
            commit=commit,
        )
        self._write_snapshot(manifest, commit)
        self._set_current(initial_snapshot.snapshot_id)
        return snapshot_helpers.snapshot_from_manifest(manifest, files)

    def _snapshot_dir(self, snapshot_id: str) -> Path:
        if not config.OBJECT_ID_RE.match(snapshot_id) or "/" in snapshot_id or "\\" in snapshot_id:
            raise StateError("SNAPSHOT_ID_INVALID", "snapshot id is not a safe local identifier")
        return self.snapshots_dir / snapshot_id

    def has_snapshot(self, snapshot_id: str) -> bool:
        try:
            directory = self._snapshot_dir(snapshot_id)
        except StateError:
            return False
        return (directory / "manifest.json").is_file() and (directory / "commit.json").is_file()

    def _write_snapshot(self, manifest: dict, commit: dict) -> None:
        snapshot_id = str(manifest["snapshot_id"])
        final_dir = self._snapshot_dir(snapshot_id)
        temp_dir = self.snapshots_dir / f".{snapshot_id}.tmp"
        if final_dir.exists():
            raise StateError("SNAPSHOT_EXISTS", "snapshot already exists", details={"snapshot_id": snapshot_id})
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        temp_dir.mkdir(parents=True)
        try:
            write_json_atomic(temp_dir / "manifest.json", manifest)
            write_json_atomic(temp_dir / "commit.json", commit)
            os.replace(temp_dir, final_dir)
        except Exception:
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
            raise

    def _set_current(self, snapshot_id: str) -> None:
        write_text_atomic(self.current_path, snapshot_id + "\n")

    def current_snapshot_id(self) -> str:
        if not self.current_path.is_file():
            raise StateError("CURRENT_POINTER_MISSING", "current pointer is missing")
        snapshot_id = self.current_path.read_text(encoding="utf-8").strip()
        if not snapshot_id:
            raise StateError("CURRENT_POINTER_INVALID", "current pointer is empty")
        if not config.OBJECT_ID_RE.match(snapshot_id):
            raise StateError("CURRENT_POINTER_INVALID", "current pointer contains an invalid snapshot id")
        if not self.has_snapshot(snapshot_id):
            raise StateError("CURRENT_POINTER_UNRESOLVED", "current pointer references a missing snapshot")
        return snapshot_id

    def _read_json(self, path: Path, code: str) -> dict:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError(code, f"cannot read {path.name}", details={"reason": exc.__class__.__name__}) from exc
        if not isinstance(value, dict):
            raise StateError(code, f"{path.name} must contain an object")
        return value

    def load_snapshot(self, snapshot_id: str) -> Snapshot:
        directory = self._snapshot_dir(snapshot_id)
        manifest = self._read_json(directory / "manifest.json", "SNAPSHOT_MANIFEST_INVALID")
        try:
            files = tuple(FileEntry.from_dict(item) for item in manifest["files"])
        except (KeyError, TypeError, ValueError) as exc:
            raise StateError("SNAPSHOT_MANIFEST_INVALID", "snapshot files are malformed", details={"snapshot_id": snapshot_id}) from exc
        if manifest.get("snapshot_id") != snapshot_id:
            raise StateError("SNAPSHOT_ID_MISMATCH", "manifest snapshot id does not match its directory")
        return snapshot_helpers.snapshot_from_manifest(manifest, files)

    def load_commit(self, snapshot_id: str) -> dict:
        return self._read_json(self._snapshot_dir(snapshot_id) / "commit.json", "SNAPSHOT_COMMIT_INVALID")

    def current_snapshot(self) -> Snapshot:
        return self.load_snapshot(self.current_snapshot_id())

    def snapshot_ids(self) -> list[str]:
        if not self.snapshots_dir.is_dir():
            return []
        return sorted(
            item.name
            for item in self.snapshots_dir.iterdir()
            if item.is_dir()
            and not item.name.startswith(".")
            and (item / "manifest.json").is_file()
        )

    def commit_snapshot(
        self,
        *,
        files: list[FileEntry],
        attempt: Attempt,
        payload_hash: str,
        base_snapshot_id: str,
        touched_partitions: list[str],
        added_paths: list[str],
        removed_paths: list[str],
        rebase: dict | None,
        parent_snapshot_id: str | None = None,
        publish_current: bool = True,
    ) -> tuple[Snapshot, dict]:
        parent = (
            self.load_snapshot(parent_snapshot_id)
            if parent_snapshot_id
            else self.current_snapshot()
        )
        sequence = parent.sequence + 1
        snapshot_id = snapshot_helpers.next_snapshot_id(
            parent.snapshot_id, sequence, attempt.attempt_id, payload_hash, attempt.logical_time
        )
        commit = snapshot_helpers.build_commit(
            snapshot_id=snapshot_id,
            parent_snapshot_id=parent.snapshot_id,
            sequence=sequence,
            logical_time=attempt.logical_time,
            attempt_id=attempt.attempt_id,
            writer_id=attempt.writer_id,
            operation_type=attempt.operation_type,
            base_snapshot_id=base_snapshot_id,
            payload_hash=payload_hash,
            touched_partitions=touched_partitions,
            added_paths=added_paths,
            removed_paths=removed_paths,
            rebase=rebase,
        )
        manifest = snapshot_helpers.build_manifest(
            snapshot_id=snapshot_id,
            parent_snapshot_id=parent.snapshot_id,
            sequence=sequence,
            logical_time=attempt.logical_time,
            files=files,
            commit=commit,
        )
        self._write_snapshot(manifest, commit)
        if publish_current:
            self._set_current(snapshot_id)
        return snapshot_helpers.snapshot_from_manifest(manifest, files), commit

    def prepare_snapshot(
        self,
        *,
        files: list[FileEntry],
        attempt: Attempt,
        payload_hash: str,
        base_snapshot_id: str,
        touched_partitions: list[str],
        added_paths: list[str],
        removed_paths: list[str],
        rebase: dict | None,
    ) -> tuple[dict, dict]:
        """Build a manifest/commit pair for the attempt without touching disk."""
        parent = self.current_snapshot()
        sequence = parent.sequence + 1
        snapshot_id = snapshot_helpers.next_snapshot_id(
            parent.snapshot_id, sequence, attempt.attempt_id, payload_hash, attempt.logical_time
        )
        commit = snapshot_helpers.build_commit(
            snapshot_id=snapshot_id,
            parent_snapshot_id=parent.snapshot_id,
            sequence=sequence,
            logical_time=attempt.logical_time,
            attempt_id=attempt.attempt_id,
            writer_id=attempt.writer_id,
            operation_type=attempt.operation_type,
            base_snapshot_id=base_snapshot_id,
            payload_hash=payload_hash,
            touched_partitions=touched_partitions,
            added_paths=added_paths,
            removed_paths=removed_paths,
            rebase=rebase,
        )
        manifest = snapshot_helpers.build_manifest(
            snapshot_id=snapshot_id,
            parent_snapshot_id=parent.snapshot_id,
            sequence=sequence,
            logical_time=attempt.logical_time,
            files=files,
            commit=commit,
        )
        return manifest, commit

    def publish_prepared(self, manifest: dict, commit: dict) -> Snapshot:
        """Write the immutable snapshot directory without moving current."""
        self._write_snapshot(manifest, commit)
        files = tuple(FileEntry.from_dict(item) for item in manifest["files"])
        return snapshot_helpers.snapshot_from_manifest(manifest, files)

    def publish_current(self, snapshot_id: str) -> None:
        self._set_current(snapshot_id)

    def remove_snapshot(self, snapshot_id: str) -> None:
        """Delete one immutable snapshot directory; only recovery calls this."""
        directory = self._snapshot_dir(snapshot_id)
        if directory.exists():
            shutil.rmtree(directory)

    def chain(self, current_id: str | None = None) -> list[Snapshot]:
        snapshot_id = current_id or self.current_snapshot_id()
        result: list[Snapshot] = []
        seen: set[str] = set()
        while snapshot_id:
            if snapshot_id in seen:
                raise StateError("SNAPSHOT_CYCLE", "snapshot parent chain contains a cycle")
            seen.add(snapshot_id)
            snapshot = self.load_snapshot(snapshot_id)
            result.append(snapshot)
            snapshot_id = snapshot.parent_snapshot_id
        return result

    def is_ancestor(self, ancestor_id: str, descendant_id: str | None = None) -> bool:
        current = descendant_id or self.current_snapshot_id()
        seen: set[str] = set()
        while current:
            if current in seen:
                return False
            if current == ancestor_id:
                return True
            seen.add(current)
            current = self.load_snapshot(current).parent_snapshot_id
        return False

    def changes_since(self, base_id: str, current_id: str | None = None) -> list[dict]:
        current = current_id or self.current_snapshot_id()
        newer: list[str] = []
        seen: set[str] = set()
        cursor = current
        while cursor and cursor != base_id:
            if cursor in seen:
                raise StateError("SNAPSHOT_CYCLE", "snapshot parent chain contains a cycle")
            seen.add(cursor)
            newer.append(cursor)
            cursor = self.load_snapshot(cursor).parent_snapshot_id
        if cursor != base_id:
            raise StateError("BASE_NOT_ANCESTOR", "base snapshot is not an ancestor of current")
        return [self.load_commit(item) for item in reversed(newer)]

    def verify(self) -> dict:
        from lakecommit.verification import verify_store

        return verify_store(self)
