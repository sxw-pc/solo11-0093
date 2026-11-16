import json

from lakecommit import snapshot as snapshot_helpers
from lakecommit.errors import StateError

def test_publish_writes_manifest_commit_and_pointer(bench):
    decision = bench.engine.submit(bench.bundle.attempts[0])
    assert decision.status == "accepted"
    snapshot_id = decision.resulting_snapshot_id
    directory = bench.store.snapshots_dir / snapshot_id
    assert (directory / "manifest.json").is_file()
    assert (directory / "commit.json").is_file()
    assert bench.store.current_snapshot_id() == snapshot_id
    snapshot = bench.store.load_snapshot(snapshot_id)
    assert snapshot.sequence == 1
    assert snapshot.parent_snapshot_id == "snap-0000"
    assert snapshot.manifest_hash == snapshot_helpers.manifest_hash(snapshot.files)
    commit = bench.store.load_commit(snapshot_id)
    assert commit["attempt_id"] == "a-alpha-001"
    assert commit["touched_partitions"] == ["region=us"]

def test_chain_ancestry_and_changes_are_deterministic(bench):
    first = bench.engine.submit(bench.bundle.attempts[0])
    second_doc = bench.bundle.attempts[0].to_dict()
    second_doc["attempt_id"] = "a-alpha-001b"
    second_doc["idempotency_key"] = "idem-alpha-001b"
    second_doc["logical_time"] = "2024-01-01T00:01:30Z"
    second_doc["operation"] = {
        "type": "add_files",
        "files": [
            {
                "path": "data/region=ca/part-0002.parquet",
                "partition": {"region": "ca"},
                "size_bytes": 10,
                "row_count": 1,
                "content_hash": "d" * 64,
            }
        ],
    }
    second = bench.engine.submit_document(second_doc)
    assert second.status == "accepted"
    ids = [item.snapshot_id for item in bench.store.chain()]
    assert ids[0] == second.resulting_snapshot_id
    assert ids[-1] == "snap-0000"
    assert len(ids) == 3
    assert bench.store.is_ancestor("snap-0000", ids[0])
    assert bench.store.is_ancestor(first.resulting_snapshot_id, ids[0])
    changes = bench.store.changes_since("snap-0000", ids[0])
    assert [item["attempt_id"] for item in changes] == ["a-alpha-001", "a-alpha-001b"]

def test_verify_detects_tampered_manifest_hash(bench):
    decision = bench.engine.submit(bench.bundle.attempts[0])
    manifest_path = bench.store.snapshots_dir / decision.resulting_snapshot_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["content_hash"] = "f" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    verification = bench.store.verify()
    assert verification["ok"] is False
    assert "MANIFEST_HASH_MISMATCH" in {item["code"] for item in verification["errors"]}
    assert any(not check["ok"] for check in verification["checks"])

def test_verify_detects_unresolvable_current_pointer(bench):
    bench.store._set_current("snap-does-not-exist")
    verification = bench.store.verify()
    assert verification["ok"] is False
    assert verification["errors"][0]["code"] == "CURRENT_POINTER_UNRESOLVED"

def test_unknown_snapshot_is_not_loadable(bench):
    try:
        bench.store.load_snapshot("snap-9999")
    except StateError as exc:
        assert exc.code in {"SNAPSHOT_MANIFEST_INVALID", "CURRENT_POINTER_UNRESOLVED"}
    else:
        raise AssertionError("missing snapshot should not load")
