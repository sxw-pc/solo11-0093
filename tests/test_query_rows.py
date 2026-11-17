import hashlib
from decimal import Decimal
from pathlib import Path

import pytest

from lakecommit.models import FileEntry, Snapshot
from lakecommit.query_rows import read_snapshot_rows
from lakecommit.store import SnapshotStore


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "query_rows"


def make_store(tmp_path):
    files = []
    schemas = {}
    for name, schema in (("v1.jsonl", "cents-v1"), ("v2.jsonl", "dollars-v2")):
        rel = f"data/region=us/{name}"
        content = (FIXTURES / rel).read_bytes()
        files.append(FileEntry(path=rel, partition={"region": "us"}, size_bytes=len(content), row_count=2,
                               content_hash=hashlib.sha256(content).hexdigest()))
        schemas[rel] = schema
    store = SnapshotStore(tmp_path)
    store.initialize(Snapshot(snapshot_id="query-root", parent_snapshot_id=None, sequence=0,
                              logical_time="2025-01-01T00:00:00Z", files=tuple(files)), ("region",))
    return store, schemas


def test_committed_reader_normalizes_two_file_schemas(tmp_path):
    store, schemas = make_store(tmp_path)
    result = read_snapshot_rows(store, FIXTURES, "query-root", schemas, minimum_amount=Decimal("10"))
    assert [row["order_id"] for row in result["rows"]] == ["o-02", "o-03", "o-04"]
    assert result["files_read"] == ["data/region=us/v1.jsonl", "data/region=us/v2.jsonl"]


def test_reader_rejects_changed_bytes(tmp_path):
    store, schemas = make_store(tmp_path)
    content = tmp_path / "data" / "region=us"
    content.mkdir(parents=True)
    for path in (FIXTURES / "data" / "region=us").iterdir():
        (content / path.name).write_bytes(path.read_bytes())
    (content / "v1.jsonl").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="content does not match committed"):
        read_snapshot_rows(store, tmp_path, "query-root", schemas, minimum_amount=Decimal("10"))
