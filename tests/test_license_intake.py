import json
from pathlib import Path
from lakecommit.license_intake import preview_single_region


def test_license_regions_are_actual_committed_snapshot_partitions():
    root=Path(__file__).parents[1]
    data=json.loads((root/"fixtures/license_market/intake.json").read_text())
    table=json.loads((root/"fixtures/tables/lake_sales_orders.json").read_text())
    snapshot=table["initial_snapshot"]
    assert data["snapshot_id"]==snapshot["snapshot_id"]
    assert data["table"]==table["table_name"]
    assert set(data["regions"])=={f["partition"]["region"] for f in snapshot["files"]}
    assert all(len(f["content_hash"])==64 for f in snapshot["files"])
    assert {r:sum(f["row_count"] for f in snapshot["files"] if f["partition"]["region"]==r) for r in data["regions"]}=={"ca":20,"eu":70,"us":160}
    assert set(data["owners"])==set(data["regions"])


def test_single_region_preview_misses_bundle():
    root=Path(__file__).parents[1]
    data=json.loads((root/"fixtures/license_market/intake.json").read_text())
    by={b["bid_id"]:b for b in data["bids"]}
    winners=preview_single_region(data)
    assert winners==["b6","b4","b7"]
    assert sum(by[i]["value"] for i in winners)==26
    assert by["b1"]["value"]+by["b4"]["value"]==25
