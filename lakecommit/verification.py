"""Read-only snapshot-chain integrity checks (kept out of the store hot path)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from lakecommit import snapshot as snapshot_helpers
from lakecommit.errors import StateError
from lakecommit.models import Snapshot

if TYPE_CHECKING:  # pragma: no cover - typing only
    from lakecommit.store import SnapshotStore


def verify_store(store: "SnapshotStore") -> dict:
    errors: list[dict] = []
    checks = {
        "current_pointer_resolves": True,
        "parent_chain_contiguous": True,
        "manifest_hashes_match": True,
        "commit_metadata_consistent": True,
        "file_paths_unique": True,
        "no_orphan_snapshots": True,
    }

    def fail(check: str, code: str, message: str, snapshot_id: str | None = None) -> None:
        checks[check] = False
        value: dict = {"code": code, "message": message}
        if snapshot_id:
            value["snapshot_id"] = snapshot_id
        errors.append(value)

    current_id: str | None = None
    try:
        current_id = store.current_snapshot_id()
    except StateError as exc:
        fail("current_pointer_resolves", exc.code, exc.message)
    chain: list[tuple[Snapshot, dict]] = []
    if current_id:
        seen: set[str] = set()
        cursor: str | None = current_id
        while cursor:
            if cursor in seen:
                fail("parent_chain_contiguous", "SNAPSHOT_CYCLE", "snapshot parent chain contains a cycle", cursor)
                break
            seen.add(cursor)
            try:
                snapshot = store.load_snapshot(cursor)
                commit = store.load_commit(cursor)
            except StateError as exc:
                fail("parent_chain_contiguous", exc.code, exc.message, cursor)
                break
            chain.append((snapshot, commit))
            cursor = snapshot.parent_snapshot_id
    for index, (snapshot, commit) in enumerate(chain):
        snapshot_id = snapshot.snapshot_id
        paths = [entry.path for entry in snapshot.files]
        if len(paths) != len(set(paths)):
            fail("file_paths_unique", "DUPLICATE_FILE_PATH", "snapshot contains duplicate file paths", snapshot_id)
        actual_manifest_hash = snapshot_helpers.manifest_hash(snapshot.files)
        if snapshot.manifest_hash != actual_manifest_hash:
            fail("manifest_hashes_match", "MANIFEST_HASH_MISMATCH", "manifest file hash differs from its content", snapshot_id)
        if commit.get("snapshot_id") != snapshot_id:
            fail("commit_metadata_consistent", "COMMIT_SNAPSHOT_MISMATCH", "commit snapshot id differs", snapshot_id)
        if commit.get("sequence") != snapshot.sequence:
            fail("commit_metadata_consistent", "COMMIT_SEQUENCE_MISMATCH", "commit sequence differs", snapshot_id)
        if commit.get("parent_snapshot_id") != snapshot.parent_snapshot_id:
            fail("commit_metadata_consistent", "COMMIT_PARENT_MISMATCH", "commit parent differs", snapshot_id)
        if index + 1 < len(chain) and snapshot.parent_snapshot_id != chain[index + 1][0].snapshot_id:
            fail("parent_chain_contiguous", "PARENT_CHAIN_BROKEN", "snapshot parent chain is broken", snapshot_id)
        if index + 1 < len(chain) and snapshot.sequence != chain[index + 1][0].sequence + 1:
            fail("parent_chain_contiguous", "SEQUENCE_GAP", "snapshot sequence is not contiguous", snapshot_id)
    if chain:
        root = chain[-1][0]
        if root.parent_snapshot_id is not None or root.sequence != 0:
            fail("parent_chain_contiguous", "ROOT_SNAPSHOT_INVALID", "chain root is not an initial snapshot", root.snapshot_id)
    chain_ids = {snapshot.snapshot_id for snapshot, _ in chain}
    orphans = sorted(set(store.snapshot_ids()) - chain_ids)
    for orphan in orphans:
        fail("no_orphan_snapshots", "ORPHAN_SNAPSHOT", "snapshot directory is not reachable from current", orphan)
    return {
        "ok": not errors,
        "current_snapshot_id": current_id,
        "snapshot_count": len(chain),
        "checks": [{"name": name, "ok": ok} for name, ok in checks.items()],
        "errors": errors,
    }
