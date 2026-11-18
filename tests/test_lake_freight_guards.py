import hashlib
import json
import shutil

import pytest

from lakecommit.freight_contracts import enforce_contract
from lakecommit.freight_intake import FreightSettlementError, settle
from lakecommit.freight_routes import FreightRouteError, RouteNetwork
from lakecommit.models import FileEntry, Snapshot
from lakecommit.store import SnapshotStore

from freight_settlement_helpers import (
    FIXTURE,
    FIXTURES,
    MERCHANTS,
    REL_FILES,
    SCHEMAS,
    rebuild_store,
    rewrite_v1,
    settle_fixture,
)


def test_snapshot_tampering_blocks_invoices(tmp_path):
    with pytest.raises(ValueError, match="content does not match committed"):
        settle_fixture(tmp_path, tamper="after_commit")


def test_mixed_schemas_are_normalized_not_added_raw(tmp_path):
    settlement, _, _ = settle_fixture(tmp_path)
    amounts = {item["merchant_id"]: item["order_amount"] for item in settlement["merchants"]}
    assert amounts == {"m-A": "9", "m-B": "11", "m-C": "10.00"}
    weights = settlement["contracts"]["value_share"]["weights_cents"]
    assert weights == {"m-A": 900, "m-B": 1100, "m-C": 1000}
    raw = [json.loads(line) for line in (FIXTURES / "data/region=us/v1.jsonl").read_text().splitlines()]
    assert any(row.get("amount_cents") == 900 for row in raw)
    raw_v2 = [json.loads(line) for line in (FIXTURES / "data/region=us/v2.jsonl").read_text().splitlines()]
    assert any(row.get("amount_dollars") == "10.00" for row in raw_v2)


def test_duplicate_merchant_and_order_ids_are_rejected(tmp_path):
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data["merchants"][1]["merchant_id"] = "m-A"
    with pytest.raises(FreightSettlementError, match="duplicate merchant_id"):
        settle_fixture(tmp_path, data)
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data["merchants"][2]["order_id"] = "o-01"
    with pytest.raises(FreightSettlementError, match="duplicate order_id"):
        settle_fixture(tmp_path, data)


def test_duplicate_order_rows_in_snapshot_are_rejected(tmp_path):
    _, _, content_root = settle_fixture(tmp_path / "base")
    v1 = content_root / "data/region=us/v1.jsonl"
    lines = [json.loads(line) for line in v1.read_text(encoding="utf-8").splitlines() if line.strip()]
    lines.append(dict(lines[0]))
    v1.write_text("\n".join(json.dumps(row) for row in lines) + "\n", encoding="utf-8")
    rebuilt, schemas = rebuild_store(tmp_path / "dup", content_root)
    with pytest.raises(FreightSettlementError, match="duplicate order_id"):
        settle(rebuilt, content_root, "query-root", schemas, json.loads(FIXTURE.read_text()))


def test_order_outside_committed_snapshot_is_rejected(tmp_path):
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data["merchants"][0]["order_id"] = "o-999"
    with pytest.raises(FreightSettlementError, match="not present in committed snapshot"):
        settle_fixture(tmp_path, data)


def test_missing_edge_negative_distance_and_capacity_shortage_rejected(tmp_path):
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    del data["distance"]["A"]["B"]
    with pytest.raises(FreightSettlementError, match="missing directed edge"):
        settle_fixture(tmp_path, data)
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data["distance"]["B"]["A"] = -3
    with pytest.raises(FreightSettlementError, match="negative distance"):
        settle_fixture(tmp_path, data)
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data["merchants"][0]["units"] = 3
    with pytest.raises(FreightSettlementError, match="over capacity"):
        settle_fixture(tmp_path, data)


def test_route_validator_rejects_unknown_merchant_and_bad_capacity_directly():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data["vehicle_capacity"] = 4
    with pytest.raises(FreightRouteError, match="exceeds bound"):
        RouteNetwork(data)
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data["vehicle_capacity"] = 0
    with pytest.raises(FreightRouteError, match="positive integer"):
        RouteNetwork(data)


def test_fractional_edge_is_rejected_rather_than_silently_truncated():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data["distance"]["A"]["B"] = 2.5
    with pytest.raises(FreightRouteError, match="missing directed edge"):
        RouteNetwork(data)


def test_verified_amount_change_causally_changes_value_bills(tmp_path):
    settlement, _, content_root = settle_fixture(tmp_path)
    before = settlement["contracts"]["value_share"]["bills_cents"]
    rewrite_v1(content_root, "o-02", 1200)
    rebuilt, schemas = rebuild_store(tmp_path, content_root)
    changed = settle(rebuilt, content_root, "query-root", schemas, json.loads(FIXTURE.read_text()))
    after = changed["contracts"]["value_share"]["bills_cents"]
    assert before != after
    assert after["m-B"] > before["m-B"]
    assert sum(after.values()) == 3700
    assert changed["contracts"]["marginal_share"]["bills_cents"] == {
        "m-A": 1300, "m-B": 1200, "m-C": 1200,
    }
    assert changed["file_identities"][0]["content_hash"] != settlement["file_identities"][0]["content_hash"]


def test_amount_weight_that_breaks_solo_bound_refuses_instead_of_truncating(tmp_path):
    _, _, content_root = settle_fixture(tmp_path)
    rewrite_v1(content_root, "o-02", 2200)
    rebuilt, schemas = rebuild_store(tmp_path, content_root, name="rejected")
    with pytest.raises(FreightSettlementError, match="solo upper bound"):
        settle(rebuilt, content_root, "query-root", schemas, json.loads(FIXTURE.read_text()))


def test_contract_enforcer_rejects_negative_and_non_conserving_bills():
    with pytest.raises(Exception, match="solo upper bound"):
        enforce_contract(
            "value_share", MERCHANTS,
            {"m-A": 1110, "m-B": 1357, "m-C": 1233},
            {"m-A": 500, "m-B": 1800, "m-C": 1600}, 3700,
        )
    with pytest.raises(Exception, match="conserve"):
        enforce_contract(
            "value_share", MERCHANTS,
            {"m-A": 1110, "m-B": 1357, "m-C": 1232},
            {"m-A": 2000, "m-B": 1800, "m-C": 1600}, 3700,
        )


def test_snapshot_identity_metadata_is_emitted(tmp_path):
    settlement, store, _ = settle_fixture(tmp_path)
    snapshot = store.load_snapshot("query-root")
    entries = {entry.path: entry for entry in snapshot.files}
    for identity in settlement["file_identities"]:
        entry = entries[identity["path"]]
        assert identity["size_bytes"] == entry.size_bytes
        assert identity["row_count"] == entry.row_count
        assert identity["content_hash"] == entry.content_hash
        assert len(identity["content_hash"]) == 64
    assert settlement["snapshot_id"] == "query-root"


def test_cli_emits_settlement_and_rejects_invalid_fixture(tmp_path, capsys):
    from lakecommit.freight_intake import main

    rc = main([
        "--fixture", str(FIXTURE),
        "--content-root", str(FIXTURES),
        "--workdir", str(tmp_path / "cli-store"),
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["grand_fleet"]["cost"] == 37
    bad_fixture = tmp_path / "bad-intake.json"
    broken = json.loads(FIXTURE.read_text(encoding="utf-8"))
    del broken["distance"]["A"]["B"]
    bad_fixture.write_text(json.dumps(broken), encoding="utf-8")
    rc = main([
        "--fixture", str(bad_fixture),
        "--content-root", str(FIXTURES),
        "--workdir", str(tmp_path / "cli-bad"),
    ])
    assert rc == 3
    error = json.loads(capsys.readouterr().out)
    assert error["error"]["code"] == "FREIGHT_SETTLEMENT_REJECTED"
    assert "missing directed edge" in error["error"]["message"]


def test_store_with_wrong_hash_is_rejected_at_read_time(tmp_path):
    content_root = tmp_path / "content"
    for rel in REL_FILES:
        target = content_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURES / rel, target)
    entries = []
    for index, rel in enumerate(REL_FILES):
        content = (content_root / rel).read_bytes()
        digest = hashlib.sha256(b"wrong" if index == 0 else content).hexdigest()
        entries.append(FileEntry(path=rel, partition={"region": "us"}, size_bytes=len(content),
                                 row_count=2, content_hash=digest))
    store = SnapshotStore(tmp_path / "bad-store")
    store.initialize(Snapshot(snapshot_id="query-root", parent_snapshot_id=None, sequence=0,
                              logical_time="2025-01-01T00:00:00Z", files=tuple(entries)), ("region",))
    with pytest.raises(ValueError, match="content does not match committed"):
        settle(store, content_root, "query-root", SCHEMAS, json.loads(FIXTURE.read_text()))
