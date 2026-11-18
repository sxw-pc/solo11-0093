import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest

from lakecommit import loader
from lakecommit.carbon_intake import main, plan_certificates, preview_hourly_offsets
from lakecommit.errors import InputError
from lakecommit.models import Attempt, FileEntry, Snapshot
from lakecommit.store import SnapshotStore

ROOT = Path(__file__).resolve().parents[1]
INTAKE = ROOT / "fixtures/carbon/intake.json"
TABLE = ROOT / "fixtures/tables/lake_sales_orders.json"


def load_intake():
    return json.loads(INTAKE.read_text())


def make_store(tmp_path, files=None, snapshot_id=None):
    table, initial = loader.load_table(TABLE)
    if files is not None or snapshot_id is not None:
        initial = Snapshot(
            snapshot_id=snapshot_id or initial.snapshot_id,
            parent_snapshot_id=None,
            sequence=0,
            logical_time=initial.logical_time,
            files=tuple(files) if files is not None else initial.files,
        )
    store = SnapshotStore(tmp_path)
    store.initialize(initial, table.partition_columns)
    return store


def commit_file_correction(store, base_id, path, *, size_bytes, content_hash):
    parent = store.load_snapshot(base_id)
    files = [
        FileEntry(
            path=entry.path,
            partition=dict(entry.partition),
            size_bytes=size_bytes if entry.path == path else entry.size_bytes,
            row_count=entry.row_count,
            content_hash=content_hash if entry.path == path else entry.content_hash,
        )
        for entry in parent.files
    ]
    attempt = Attempt(
        attempt_id="att-correct-0001", writer_id="carbon-test", idempotency_key="carbon-correct-0001",
        base_snapshot_id=base_id, logical_time="2024-01-02T00:00:00Z",
        operation={"type": "replace_file", "path": path},
    )
    snapshot, _commit = store.commit_snapshot(
        files=files, attempt=attempt, payload_hash="f" * 64, base_snapshot_id=base_id,
        touched_partitions=["region=us"], added_paths=[], removed_paths=[], rebase=None,
    )
    return snapshot.snapshot_id


def allocations_by_pair(contract):
    return {(a["certificate_id"], a["load_id"]): Decimal(a["units"]) for a in contract["allocations"]}


def rejection_code(store, data):
    with pytest.raises(InputError) as err:
        plan_certificates(store, data)
    return err.value.code


def test_metered_reads_bind_to_initial_committed_files():
    data = load_intake()
    table = json.loads(TABLE.read_text())
    snapshot = table["initial_snapshot"]
    assert data["snapshot_id"] == snapshot["snapshot_id"]
    by = {f["path"]: f for f in snapshot["files"]}
    assert {l["file_path"] for l in data["loads"]} == set(by)
    assert all(by[l["file_path"]]["partition"]["region"] == l["region"] for l in data["loads"])
    assert {l["load_id"]: Decimal(by[l["file_path"]]["size_bytes"]) / 100 for l in data["loads"]} == {
        "ca-h2": Decimal("3.2"), "eu-h1": Decimal("9.1"),
        "us-h1": Decimal("12"), "us-h2": Decimal("7.8")}


def test_first_fit_wastes_flexible_credit_on_early_us_load():
    data = load_intake()
    table = json.loads(TABLE.read_text())
    sizes = {f["path"]: f["size_bytes"] for f in table["initial_snapshot"]["files"]}
    result = preview_hourly_offsets(data, sizes)
    assert Decimal(result["uncovered"]["us-h2"]) == Decimal("7.8")
    assert Decimal(result["uncovered"]["us-h1"]) == 0


def test_plan_meters_loads_from_committed_manifest(tmp_path):
    report = plan_certificates(make_store(tmp_path), load_intake())
    assert report["snapshot_id"] == "snap-0000"
    assert report["synthetic"] is True
    by_id = {l["load_id"]: l for l in report["loads"]}
    assert by_id["ca-h2"]["units"] == "3.2"
    assert by_id["eu-h1"]["units"] == "9.1"
    assert by_id["us-h1"]["units"] == "12"
    assert by_id["us-h2"]["units"] == "7.8"
    assert by_id["us-h2"]["size_bytes"] == 780
    assert by_id["us-h2"]["content_hash"] == "b" * 64


def test_hourly_region_optimal_shortfall_refutes_greedy_evidence(tmp_path):
    store = make_store(tmp_path)
    data = load_intake()
    report = plan_certificates(store, data)
    hourly = report["contracts"]["hourly_region"]
    assert hourly["issued"] is False
    assert Decimal(hourly["total_uncovered"]) == Decimal("3.8")
    assert Decimal(hourly["max_uncovered"]) == Decimal("3.50")
    sizes = {l["file_path"]: l["size_bytes"] for l in report["loads"]}
    greedy = preview_hourly_offsets(data, sizes)
    assert Decimal(greedy["uncovered"]["us-h2"]) == Decimal("7.8")
    # the first-fit preview overstates the true minimum uncovered remainder by 4.0
    assert Decimal(greedy["uncovered"]["us-h2"]) - Decimal(hourly["total_uncovered"]) == Decimal("4.0")


def test_hourly_region_flexible_certificate_is_not_trapped_on_h1(tmp_path):
    report = plan_certificates(make_store(tmp_path), load_intake())
    hourly = report["contracts"]["hourly_region"]
    assert allocations_by_pair(hourly) == {
        ("c-ca-h2", "ca-h2"): Decimal("3.2"),
        ("c-eu-h1", "eu-h1"): Decimal("9.1"),
        ("c-flex-us", "us-h1"): Decimal("4"),
        ("c-flex-us", "us-h2"): Decimal("4"),
        ("c-h1-us", "us-h1"): Decimal("8"),
    }
    # c-flex-us keeps the share only it can reach on h2 instead of draining into h1
    assert Decimal(hourly["uncovered_loads"]["us-h1"]) == 0
    assert Decimal(hourly["uncovered_loads"]["us-h2"]) == Decimal("3.8")


def test_annual_pool_issues_with_zero_uncovered(tmp_path):
    report = plan_certificates(make_store(tmp_path), load_intake())
    annual = report["contracts"]["annual_pool"]
    assert annual["issued"] is True
    assert Decimal(annual["total_uncovered"]) == 0
    assert Decimal(annual["total_load"]) == Decimal("32.1")
    assert Decimal(annual["total_covered"]) == Decimal("32.1")
    pairs = allocations_by_pair(annual)
    # region-locked certificates cover their own ca/eu loads first
    assert pairs[("c-ca-h2", "ca-h2")] == Decimal("3.2")
    assert pairs[("c-eu-h1", "eu-h1")] == Decimal("9.1")
    # the eu surplus certificate transfers across regions/hours into us
    assert pairs[("c-extra-eu", "us-h1")] == Decimal("4")
    assert pairs[("c-flex-us", "us-h1")] == Decimal("8")
    assert pairs[("c-h1-us", "us-h2")] == Decimal("7.8")
    assert Decimal(annual["certificate_remaining"]["c-h1-us"]) == Decimal("0.2")


def test_two_contracts_disagree_on_the_same_batch(tmp_path):
    report = plan_certificates(make_store(tmp_path), load_intake())
    assert report["contracts"]["hourly_region"]["issued"] is False
    assert report["contracts"]["annual_pool"]["issued"] is True


def test_certificate_units_are_conserved_across_loads(tmp_path):
    report = plan_certificates(make_store(tmp_path), load_intake())
    certs = {c["certificate_id"]: Decimal(c["units"]) for c in report["certificates"]}
    for contract in report["contracts"].values():
        spent = {}
        for alloc in contract["allocations"]:
            spent[alloc["certificate_id"]] = spent.get(alloc["certificate_id"], Decimal(0)) + Decimal(alloc["units"])
        for cert_id, units in spent.items():
            assert units <= certs[cert_id]
        total = Decimal(contract["total_covered"]) + Decimal(contract["total_uncovered"])
        assert total == Decimal(contract["total_load"])
    hourly = allocations_by_pair(report["contracts"]["hourly_region"])
    assert sum(u for (c, _l), u in hourly.items() if c == "c-flex-us") == Decimal("8")


def test_hourly_region_never_treats_annual_transfer_as_hour_eligible(tmp_path):
    report = plan_certificates(make_store(tmp_path), load_intake())
    hourly = report["contracts"]["hourly_region"]
    certs = {c["certificate_id"]: c for c in report["certificates"]}
    loads = {l["load_id"]: l for l in report["loads"]}
    for cert_id, load_id in allocations_by_pair(hourly):
        assert certs[cert_id]["region"] == loads[load_id]["region"]
        assert loads[load_id]["hour"] in certs[cert_id]["eligible_hours"]
    # c-extra-eu has annual_transfer=true but no eligible hours; counting it as
    # hour-eligible would game the 3.50 threshold, so it must stay out entirely
    assert all(cert_id != "c-extra-eu" for cert_id, _load_id in allocations_by_pair(hourly))
    assert Decimal(hourly["certificate_remaining"]["c-extra-eu"]) == Decimal("4")


def test_threshold_boundary_at_3_50(tmp_path):
    store = make_store(tmp_path)
    data = load_intake()
    exact_id = commit_file_correction(
        store, data["snapshot_id"], "data/region=us/part-0002.parquet", size_bytes=750, content_hash="1" * 64)
    over_id = commit_file_correction(
        store, exact_id, "data/region=us/part-0002.parquet", size_bytes=751, content_hash="2" * 64)
    at_limit = plan_certificates(store, {**data, "snapshot_id": exact_id})
    beyond = plan_certificates(store, {**data, "snapshot_id": over_id})
    assert Decimal(at_limit["contracts"]["hourly_region"]["total_uncovered"]) == Decimal("3.50")
    assert at_limit["contracts"]["hourly_region"]["issued"] is True
    assert Decimal(beyond["contracts"]["hourly_region"]["total_uncovered"]) == Decimal("3.51")
    assert beyond["contracts"]["hourly_region"]["issued"] is False


def test_file_correction_reversions_loads_allocations_and_issuance(tmp_path):
    store = make_store(tmp_path)
    data = load_intake()
    before = plan_certificates(store, data)
    corrected_id = commit_file_correction(
        store, data["snapshot_id"], "data/region=us/part-0002.parquet", size_bytes=100, content_hash="9" * 64)
    assert corrected_id != data["snapshot_id"]
    after = plan_certificates(store, {**data, "snapshot_id": corrected_id})
    # the corrected file is a new version: new snapshot, new manifest, new load
    assert after["manifest_hash"] != before["manifest_hash"]
    before_load = {l["load_id"]: l for l in before["loads"]}["us-h2"]
    after_load = {l["load_id"]: l for l in after["loads"]}["us-h2"]
    assert (before_load["size_bytes"], before_load["units"]) == (780, "7.8")
    assert (after_load["size_bytes"], after_load["units"]) == (100, "1")
    assert after_load["content_hash"] == "9" * 64
    # the smaller load causally re-allocates certificates and flips issuance
    assert before["contracts"]["hourly_region"]["issued"] is False
    assert after["contracts"]["hourly_region"]["issued"] is True
    assert Decimal(after["contracts"]["hourly_region"]["total_uncovered"]) == 0
    assert allocations_by_pair(after["contracts"]["hourly_region"])[("c-flex-us", "us-h2")] == Decimal("1")
    assert after["contracts"]["annual_pool"]["issued"] is True


def test_unknown_snapshot_file_and_partition_mismatch_rejected(tmp_path):
    store = make_store(tmp_path)
    data = load_intake()
    assert rejection_code(store, {**data, "snapshot_id": "snap-9999"}) == "CARBON_UNKNOWN_SNAPSHOT"
    bad_file = copy.deepcopy(data)
    bad_file["loads"][0]["file_path"] = "data/region=ca/part-9999.parquet"
    assert rejection_code(store, bad_file) == "CARBON_UNKNOWN_FILE"
    bad_region = copy.deepcopy(data)
    bad_region["loads"][0]["region"] = "us"  # ca-h2's file is committed under region=ca
    assert rejection_code(store, bad_region) == "CARBON_PARTITION_MISMATCH"


def test_duplicate_ids_rejected(tmp_path):
    store = make_store(tmp_path)
    dup_load = load_intake()
    dup_load["loads"][1]["load_id"] = dup_load["loads"][0]["load_id"]
    assert rejection_code(store, dup_load) == "CARBON_DUPLICATE_LOAD_ID"
    dup_cert = load_intake()
    dup_cert["certificates"][1]["certificate_id"] = dup_cert["certificates"][0]["certificate_id"]
    assert rejection_code(store, dup_cert) == "CARBON_DUPLICATE_CERTIFICATE_ID"


def test_negative_units_rejected(tmp_path):
    store = make_store(tmp_path)
    data = load_intake()
    data["certificates"][0]["units"] = "-1.00"
    assert rejection_code(store, data) == "CARBON_NEGATIVE_UNITS"


def test_unknown_region_empty_snapshot_and_empty_loads_rejected(tmp_path):
    store = make_store(tmp_path)
    bad_region = load_intake()
    bad_region["certificates"][0]["region"] = "zz"
    assert rejection_code(store, bad_region) == "CARBON_UNKNOWN_REGION"
    empty_store = make_store(tmp_path / "empty", files=[], snapshot_id="snap-empty")
    assert rejection_code(empty_store, {**load_intake(), "snapshot_id": "snap-empty"}) == "CARBON_EMPTY_SNAPSHOT"
    assert rejection_code(store, {**load_intake(), "loads": []}) == "CARBON_NO_LOADS"


def test_scale_bounds_rejected(tmp_path):
    store = make_store(tmp_path)
    data = load_intake()
    data["loads"] = [dict(data["loads"][0], load_id=f"extra-{i}") for i in range(9)]
    assert rejection_code(store, data) == "CARBON_SCALE_EXCEEDED"


def test_input_reordering_does_not_change_decisions(tmp_path):
    store = make_store(tmp_path)
    data = load_intake()
    shuffled = copy.deepcopy(data)
    shuffled["loads"] = list(reversed(shuffled["loads"]))
    shuffled["certificates"] = list(reversed(shuffled["certificates"]))
    shuffled["contracts"] = dict(reversed(list(shuffled["contracts"].items())))
    assert plan_certificates(store, shuffled) == plan_certificates(store, data)


def test_cli_issues_formal_decisions(capsys, tmp_path):
    code = main(["--fixture", str(INTAKE), "--table", str(TABLE)])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["snapshot_id"] == "snap-0000"
    assert out["contracts"]["hourly_region"]["issued"] is False
    assert Decimal(out["contracts"]["hourly_region"]["total_uncovered"]) == Decimal("3.8")
    assert out["contracts"]["annual_pool"]["issued"] is True
    assert Decimal(out["contracts"]["annual_pool"]["total_uncovered"]) == 0
    assert out["legacy_greedy_preview"]["authoritative"] is False
    assert Decimal(out["legacy_greedy_preview"]["uncovered"]["us-h2"]) == Decimal("7.8")
    make_store(tmp_path)
    assert main(["--fixture", str(INTAKE), "--workdir", str(tmp_path)]) == 0
    again = json.loads(capsys.readouterr().out)
    assert again["contracts"] == out["contracts"]
