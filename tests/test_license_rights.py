import copy
import json
import tempfile
from pathlib import Path

import pytest

from lakecommit import loader
from lakecommit.errors import SchemaError
from lakecommit.license_market import ROW_WEIGHTED, STANDALONE_FLOOR, RightsCatalog
from lakecommit.license_royalty import row_shares, standalone_payouts
from lakecommit.license_sale import money, sell, verify_certificate
from lakecommit.license_market import parse_intake
from lakecommit.models import FileEntry, Snapshot
from lakecommit.snapshot import manifest_hash

ROOT = Path(__file__).parents[1]
INTAKE = ROOT / "fixtures" / "license_market" / "intake.json"
TABLE = ROOT / "fixtures" / "tables" / "lake_sales_orders.json"


def fixture_data():
    return json.loads(INTAKE.read_text(encoding="utf-8"))


def fixture_catalog():
    table, initial = loader.load_table(TABLE)
    return RightsCatalog.from_snapshot(initial, table)


def catalog_with_files(base, files, snapshot_id="snap-0000"):
    table, _ = loader.load_table(TABLE)
    snapshot = Snapshot(snapshot_id, None, 0, base.logical_time, tuple(files), manifest_hash(files))
    return RightsCatalog.from_snapshot(snapshot, table)


def catalog_from_files(files, snapshot_id="snap-0000"):
    table, _ = loader.load_table(TABLE)
    snapshot = Snapshot(snapshot_id, None, 0, "2024-01-01T00:00:00Z", tuple(files), manifest_hash(files))
    return RightsCatalog.from_snapshot(snapshot, table)


def file_entry(region, rows, path=None):
    return FileEntry(path=path or f"data/region={region}/part-0001.parquet",
                     partition={"region": region}, size_bytes=rows * 10,
                     row_count=rows, content_hash=(region[0] * 64))


def intake(regions, owners, bids, contracts=("row_weighted", "standalone_floor"), snapshot_id="snap-0000"):
    rules = {"row_weighted": "partition_row_share",
             "standalone_floor": "owner_outside_option_then_equal_surplus"}
    return {
        "snapshot_id": snapshot_id,
        "table": "lake_sales_orders",
        "regions": list(regions),
        "owners": dict(owners),
        "bids": list(bids),
        "contracts": {name: {"royalty_rule": rules[name]} for name in contracts},
    }


def bid(bid_id, licensee, regions, value):
    return {"bid_id": bid_id, "licensee": licensee, "regions": list(regions), "value": value}


def result(data, catalog=None):
    catalog = catalog or fixture_catalog()
    return sell(catalog, data)


def contract_value(report, name):
    return report["contracts"][name]["total_value_cents"]


def test_snapshot_change_updates_rights_splits_and_retires_certificate():
    old_catalog = fixture_catalog()
    old_sale = sell(old_catalog, fixture_data())
    old_certificate = copy.deepcopy(old_sale["certificate"])
    changed_files = [
        FileEntry(path=f.path, partition=f.partition, size_bytes=f.size_bytes,
                  row_count=50 if f.partition["region"] == "ca" else f.row_count,
                  content_hash=("d" * 64) if f.partition["region"] == "ca" else f.content_hash)
        for f in old_catalog.files
    ]
    new_catalog = catalog_with_files(old_catalog, changed_files, snapshot_id="snap-0001")
    assert new_catalog.identity()["fingerprint"] != old_catalog.identity()["fingerprint"]
    assert new_catalog.identity()["manifest_hash"] != old_catalog.identity()["manifest_hash"]
    assert new_catalog.row_counts["ca"] == 50
    new_sale = sell(new_catalog, {**fixture_data(), "snapshot_id": "snap-0001"})
    weighted = new_sale["contracts"][ROW_WEIGHTED]
    assert [(p["owner"], p["amount_cents"]) for p in weighted["payouts"]] == [
        ("owner-ca", 833), ("owner-eu", 1167), ("owner-us", 500)
    ]
    assert old_sale["contracts"][ROW_WEIGHTED]["total_value_cents"] == 2500
    with pytest.raises(SchemaError) as exc:
        verify_certificate(old_certificate, new_catalog)
    assert exc.value.code == "CERTIFICATE_SNAPSHOT_MISMATCH"
    assert old_certificate["manifest_hash"] == old_catalog.manifest_hash
    verify_certificate(old_certificate, old_catalog)


def test_missing_and_added_regions_change_sellable_rights():
    old_catalog = fixture_catalog()
    ca_only = catalog_with_files(old_catalog, [f for f in old_catalog.files if f.partition["region"] == "ca"])
    assert ca_only.regions == ("ca",)
    with pytest.raises(SchemaError) as exc:
        result({**fixture_data(), "snapshot_id": "snap-0000"}, ca_only)
    assert exc.value.code == "INTAKE_REGIONS_MISMATCH"
    assert exc.value.details["not_in_snapshot"] == ["eu", "us"]
    added = old_catalog.files + (FileEntry(
        path="data/region=apac/part-0001.parquet", partition={"region": "apac"},
        size_bytes=10, row_count=5, content_hash="1" * 64),)
    apac_catalog = catalog_with_files(old_catalog, added)
    assert apac_catalog.regions == ("apac", "ca", "eu", "us")
    with pytest.raises(SchemaError) as exc:
        result(fixture_data(), apac_catalog)
    assert exc.value.code == "INTAKE_REGIONS_MISMATCH"
    assert exc.value.details["missing_from_intake"] == ["apac"]


def test_certificate_identity_mismatch_is_rejected():
    catalog = fixture_catalog()
    report = sell(catalog, fixture_data())
    certificate = report["certificate"]
    tampered = {**certificate, "rights_fingerprint": "0" * 64}
    with pytest.raises(SchemaError) as exc:
        verify_certificate(tampered, catalog)
    assert exc.value.code == "CERTIFICATE_IDENTITY_MISMATCH"
    with pytest.raises(SchemaError) as exc:
        verify_certificate({"table": "lake_sales_orders"}, catalog)
    assert exc.value.code == "CERTIFICATE_INVALID"
    with pytest.raises(SchemaError) as exc:
        verify_certificate({"snapshot_id": "snap-0000"}, catalog)
    assert exc.value.code == "CERTIFICATE_IDENTITY_MISMATCH"

def test_new_committed_snapshot_changes_rights_and_keeps_old_certificate_value():
    from lakecommit.ledger import Ledger
    from lakecommit.protocol import ProtocolEngine
    from lakecommit.store import SnapshotStore

    table, initial = loader.load_table(TABLE)
    bundle = loader.load_bundle(ROOT / "fixtures" / "scenarios" / "01_add_files_accepted.yaml")
    with tempfile.TemporaryDirectory() as workdir:
        store = SnapshotStore(workdir)
        store.reset()
        store.initialize(initial, table.partition_columns)
        engine = ProtocolEngine(bundle.table, store, Ledger(store.ledger_path, reset=True), bundle.protocol)
        decision = engine.submit(bundle.attempts[0])
        assert decision.status == "accepted"
        newer = store.load_snapshot(decision.resulting_snapshot_id)
    old_catalog = RightsCatalog.from_snapshot(initial, table)
    new_catalog = RightsCatalog.from_snapshot(newer, table)
    assert new_catalog.row_counts["us"] == 200
    assert new_catalog.regions == old_catalog.regions
    old_sale = sell(old_catalog, fixture_data())
    new_data = {**fixture_data(), "snapshot_id": new_catalog.snapshot_id}
    new_sale = sell(new_catalog, new_data)
    assert new_sale["contracts"][ROW_WEIGHTED]["payouts"][-1]["amount_cents"] == 500
    assert new_sale["certificate"]["snapshot_id"] != old_sale["certificate"]["snapshot_id"]
    assert new_sale["certificate"]["manifest_hash"] != old_sale["certificate"]["manifest_hash"]
    with pytest.raises(SchemaError):
        verify_certificate(old_sale["certificate"], new_catalog)
    assert verify_certificate(old_sale["certificate"], old_catalog)["ok"] is True
    replayed = {**{k: v for k, v in fixture_data().items()}, "snapshot_id": new_catalog.snapshot_id,
                "certificate": old_sale["certificate"]}
    with pytest.raises(SchemaError) as exc:
        sell(new_catalog, replayed)
    assert exc.value.code == "CERTIFICATE_SNAPSHOT_MISMATCH"
