import copy
import json
from pathlib import Path

import pytest

from lakecommit import loader
from lakecommit.errors import SchemaError
from lakecommit.license_intake import preview_single_region
from lakecommit.license_market import ROW_WEIGHTED, STANDALONE_FLOOR, RightsCatalog
from lakecommit.license_royalty import row_shares, standalone_payouts
from lakecommit.license_sale import money, sell
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


def test_fixture_matches_committed_snapshot_and_legacy_preview():
    data = fixture_data()
    catalog = fixture_catalog()
    assert catalog.snapshot_id == data["snapshot_id"]
    assert catalog.row_counts == {"ca": 20, "eu": 70, "us": 160}
    assert catalog.size_bytes["us"] == 1980
    assert all(len(entry.content_hash) == 64 for entry in catalog.files)
    assert preview_single_region(data) == ["b6", "b4", "b7"]


def test_row_weighted_beats_larger_licensee_set_and_misses_bundle():
    report = result(fixture_data())
    weighted = report["contracts"][ROW_WEIGHTED]
    assert [(w["bid_id"], w["licensee"]) for w in weighted["winners"]] == [("b1", "lic-1"), ("b4", "lic-4")]
    assert weighted["total_value_cents"] == 2500
    assert weighted["conservation"]["balanced"] is True
    assert weighted["candidates"][1]["bid_ids"] == ["b2", "b4", "b7"]
    assert weighted["candidates"][1]["value_cents"] == 2500
    assert weighted["candidates"][1]["licensee_count"] == 3
    assert weighted["candidate_count"] == 18
    assert weighted["unsold_regions"] == []


def test_row_weighted_payout_uses_merged_rows_and_cents():
    weighted = result(fixture_data())["contracts"][ROW_WEIGHTED]
    assert catalog_rows_are_merged()
    assert [(p["owner"], p["amount_cents"]) for p in weighted["payouts"]] == [
        ("owner-ca", 444), ("owner-eu", 1556), ("owner-us", 500)
    ]
    assert [(p["owner"], p["amount"]) for p in weighted["payouts"]] == [
        ("owner-ca", "4.44"), ("owner-eu", "15.56"), ("owner-us", "5.00")
    ]


def catalog_rows_are_merged():
    catalog = fixture_catalog()
    return catalog.row_counts["us"] == 160 and sum(1 for f in catalog.files if f.partition["region"] == "us") == 2


def test_standalone_floor_blocks_bundle_and_changes_winners():
    standalone = result(fixture_data())["contracts"][STANDALONE_FLOOR]
    assert [(w["bid_id"], w["regions"]) for w in standalone["winners"]] == [
        ("b4", ["us"]), ("b6", ["ca"])
    ]
    assert standalone["total_value_cents"] == 2100
    assert standalone["floors"]["ca"]["amount"] == "16.00"
    assert standalone["floors"]["eu"]["amount"] == "5.00"
    assert standalone["floors"]["us"]["amount"] == "5.00"
    by_id = {item["bid_id"]: item for item in standalone["bid_eligibility"]}
    assert by_id["b1"]["eligible"] is False
    assert by_id["b1"]["required_floor_cents"] == 2100
    assert by_id["b2"]["eligible"] is False and by_id["b2"]["required_floor_cents"] == 1600
    assert by_id["b6"]["eligible"] is True
    assert [(p["owner"], p["amount_cents"]) for p in standalone["payouts"]] == [
        ("owner-ca", 1600), ("owner-us", 500)
    ]
    assert standalone["unsold_regions"] == [{
        "region": "eu", "owner": "owner-eu", "floor_cents": 500, "floor": "5.00",
        "reason": "eligible_bids_blocked_by_licensee_conflict", "eligible_bids": ["b7"],
        "sellable_bids": ["b7"],
    }]


def test_bundle_cannot_pad_same_licensee_alternatives():
    weighted = result(fixture_data())["contracts"][ROW_WEIGHTED]
    for candidate in weighted["candidates"]:
        assert not {"b6", "b7"} <= set(candidate["bid_ids"])
    assert ["b4", "b6"] in [c["bid_ids"] for c in weighted["candidates"]]
    assert weighted["candidates"][0]["bid_ids"] == ["b1", "b4"]


def test_same_licensee_alternatives_are_mutually_exclusive():
    data = fixture_data()
    for name in (ROW_WEIGHTED, STANDALONE_FLOOR):
        report = result(data)["contracts"][name]
        winners = [w for w in report["winners"] if w["licensee"] == "lic-1"]
        assert len(winners) == 1
    weighted = result(data)["contracts"][ROW_WEIGHTED]
    assert "b7" not in [w["bid_id"] for w in weighted["winners"]]
    standalone = result(data)["contracts"][STANDALONE_FLOOR]
    assert "b7" not in [w["bid_id"] for w in standalone["winners"]]


def test_same_value_tie_prefers_fewer_licensees_then_stable_bid_id():
    data = intake(
        ["ca"], {"ca": "owner-ca"},
        [bid("b2", "lic-2", ["ca"], 5), bid("b1", "lic-1", ["ca"], 5)],
        contracts=("row_weighted",),
    )
    winners = result(data, catalog_from_files([file_entry("ca", 20)]))["contracts"][ROW_WEIGHTED]["candidates"]
    assert winners[0]["bid_ids"] == ["b1"] and winners[0]["winner"] is True
    assert winners[1]["bid_ids"] == ["b2"]


def test_row_share_residual_uses_stable_region_order():
    assert row_shares(1, ["ca", "eu"], {"ca": 1, "eu": 1}) == {"ca": 1, "eu": 0}
    assert sum(row_shares(100, ["ca", "eu", "us"], {"ca": 1, "eu": 1, "us": 1}).values()) == 100
    uneven = row_shares(100, ["ca", "eu", "us"], {"ca": 1, "eu": 2, "us": 3})
    assert uneven == {"ca": 17, "eu": 33, "us": 50}
    zero_rows = row_shares(100, ["ca", "eu"], {"ca": 0, "eu": 0})
    assert zero_rows == {"ca": 50, "eu": 50}
    assert sum(zero_rows.values()) == 100


def test_standalone_surplus_splits_equally_and_respects_floors():
    catalog = catalog_from_files([file_entry("ca", 20), file_entry("eu", 70)])
    data = parse_intake(intake(
        ["ca", "eu"], {"ca": "owner-ca", "eu": "owner-eu"},
        [bid("b9", "lic-9", ["ca", "eu"], 24), bid("b8", "lic-8", ["ca"], 16)],
        contracts=("standalone_floor",),
    ), catalog)
    floors = {"ca": 1600, "eu": 500, "us": 0}
    winners = [b for b in data.bids if b.bid_id == "b9"]
    payouts = standalone_payouts(data, floors, winners)
    assert [(p["owner"], p["amount_cents"]) for p in payouts] == [("owner-ca", 1750), ("owner-eu", 650)]
    assert [s["surplus_cents"] for p in payouts for s in p["sources"]] == [150, 150]


def test_rejects_bad_bids_and_bad_catalog_targets():
    catalog = fixture_catalog()
    base = intake(["ca", "eu", "us"], {"ca": "owner-ca", "eu": "owner-eu", "us": "owner-us"},
                  [bid("b1", "lic-1", ["ca"], 1)])
    for mutate, code in (
        (lambda d: d["bids"][0].update(regions=["zz"]), "BID_REGION_UNKNOWN"),
        (lambda d: d["bids"][0].update(value=-1), "BID_VALUE_INVALID"),
        (lambda d: d.update(snapshot_id="snap-9999"), "INTAKE_SNAPSHOT_MISMATCH"),
        (lambda d: d.update(regions=["ca", "eu"]), "INTAKE_REGIONS_MISMATCH"),
        (lambda d: d.update(regions=[["ca"], "eu", "us"]), "INTAKE_REGIONS_INVALID"),
        (lambda d: d["owners"].pop("us"), "INTAKE_OWNERS_INVALID"),
        (lambda d: d.update(bids=[bid(f"x{i}", f"lic-{i}", ["ca"], 1) for i in range(9)]),
         "BID_LIMIT_EXCEEDED"),
        (lambda d: d["bids"].append(bid("b1", "lic-2", ["eu"], 3)), "BID_ID_DUPLICATE"),
        (lambda d: d["bids"].append(bid("b2", "lic-1", ["ca"], 9)), "LICENSEE_OFFER_DUPLICATE"),
    ):
        data = copy.deepcopy(base)
        mutate(data)
        with pytest.raises(SchemaError) as exc:
            parse_intake(data, catalog)
        assert exc.value.code == code


def test_bid_input_order_does_not_change_outcome():
    data = fixture_data()
    first = result(data)
    shuffled = copy.deepcopy(data)
    shuffled["bids"].reverse()
    second = result(shuffled)
    assert first["contracts"] == second["contracts"]
    assert first["certificate"] == second["certificate"]
    json.dumps(first, sort_keys=True)


def test_every_winner_settles_exactly_one_owner_share_and_total():
    report = result(fixture_data())
    for name in (ROW_WEIGHTED, STANDALONE_FLOOR):
        contract = report["contracts"][name]
        conservation = contract["conservation"]
        assert conservation["balanced"] is True
        assert conservation["batch_sale_cents"] == conservation["batch_payout_cents"]
        assert sum(item["amount_cents"] for item in contract["payouts"]) == contract["total_value_cents"]
        assert all(item["balanced"] for item in conservation["per_bid"])
        assert money(contract["total_value_cents"]) == contract["total_value"]


def test_cli_prints_contract_winner_report(capsys):
    from lakecommit.license_intake import main
    assert main(["--fixture", str(INTAKE)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["contracts"]["row_weighted"]["total_value_cents"] == 2500
    assert payload["contracts"]["standalone_floor"]["total_value_cents"] == 2100

def test_standalone_surplus_stable_cent_remainder_across_three_owners():
    catalog = catalog_from_files([file_entry("ca", 1), file_entry("eu", 1), file_entry("us", 1)])
    data = intake(
        ["ca", "eu", "us"], {"ca": "owner-ca", "eu": "owner-eu", "us": "owner-us"},
        [bid("b1", "lic-1", ["ca", "eu", "us"], 16),
         bid("b2", "lic-2", ["ca"], 5), bid("b3", "lic-3", ["eu"], 5), bid("b4", "lic-4", ["us"], 5)],
        contracts=("standalone_floor",),
    )
    sale = sell(catalog, data)["contracts"][STANDALONE_FLOOR]
    assert [w["bid_id"] for w in sale["winners"]] == ["b1"]
    assert [(p["owner"], p["amount_cents"]) for p in sale["payouts"]] == [
        ("owner-ca", 534), ("owner-eu", 533), ("owner-us", 533),
    ]
    assert [source["surplus_cents"] for payout in sale["payouts"] for source in payout["sources"]] == [34, 33, 33]
    assert sale["conservation"]["balanced"] is True

def test_cli_rejects_unknown_snapshot_with_stable_exit_code(tmp_path, capsys):
    from lakecommit.license_intake import main
    payload = fixture_data()
    payload["snapshot_id"] = "snap-9999"
    fixture = tmp_path / "intake.json"
    fixture.write_text(json.dumps(payload), encoding="utf-8")
    assert main(["--fixture", str(fixture)]) == 3
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "SNAPSHOT_ID_UNKNOWN"
