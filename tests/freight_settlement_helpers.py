"""Shared helpers for snapshot-bound co-delivery settlement tests."""

import json
import shutil
from pathlib import Path

from lakecommit.freight_intake import build_ephemeral_store, settle

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "fixtures" / "lake_freight" / "intake.json"
FIXTURES = ROOT / "fixtures" / "query_rows"
REL_FILES = ("data/region=us/v1.jsonl", "data/region=us/v2.jsonl")
SCHEMAS = {"data/region=us/v1.jsonl": "cents-v1", "data/region=us/v2.jsonl": "dollars-v2"}
MERCHANTS = ("m-A", "m-B", "m-C")


def load_data():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def settle_fixture(tmp_path, data=None, *, tamper=None, snapshot_id="query-root"):
    data = load_data() if data is None else data
    content_root = tmp_path / "content"
    for rel in REL_FILES:
        target = content_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURES / rel, target)
    files = [{"path": rel, "partition": {"region": "us"}} for rel in REL_FILES]
    if tamper == "after_commit":
        store, schemas = build_ephemeral_store(tmp_path / "store", content_root, snapshot_id, files, SCHEMAS)
        path = content_root / "data/region=us/v1.jsonl"
        path.write_text(path.read_text(encoding="utf-8").replace("900", "901"), encoding="utf-8")
    else:
        if tamper is not None:
            tamper(content_root)
        store, schemas = build_ephemeral_store(tmp_path / "store", content_root, snapshot_id, files, SCHEMAS)
    return settle(store, content_root, snapshot_id, schemas, data), store, content_root


def rebuild_store(tmp_path, content_root, name="rebuilt", snapshot_id="query-root"):
    return build_ephemeral_store(
        tmp_path / name, content_root, snapshot_id,
        [{"path": rel, "partition": {"region": "us"}} for rel in REL_FILES], SCHEMAS,
    )


def rewrite_v1(content_root, order_id, amount_cents):
    v1 = content_root / "data/region=us/v1.jsonl"
    lines = [json.loads(line) for line in v1.read_text(encoding="utf-8").splitlines() if line.strip()]
    for row in lines:
        if row["order_id"] == order_id:
            row["amount_cents"] = amount_cents
    v1.write_text("\n".join(json.dumps(row) for row in lines) + "\n", encoding="utf-8")
