import json
from pathlib import Path
from decimal import Decimal
from lakecommit.carbon_intake import preview_hourly_offsets


def test_metered_reads_bind_to_initial_committed_files():
    root=Path(__file__).parents[1]
    data=json.loads((root/"fixtures/carbon/intake.json").read_text())
    table=json.loads((root/"fixtures/tables/lake_sales_orders.json").read_text())
    snapshot=table["initial_snapshot"]
    assert data["snapshot_id"]==snapshot["snapshot_id"]
    by={f["path"]:f for f in snapshot["files"]}
    assert {l["file_path"] for l in data["loads"]}==set(by)
    assert all(by[l["file_path"]]["partition"]["region"]==l["region"] for l in data["loads"])
    assert {l["load_id"]:Decimal(by[l["file_path"]]["size_bytes"])/100 for l in data["loads"]}=={
        "ca-h2":Decimal("3.2"),"eu-h1":Decimal("9.1"),
        "us-h1":Decimal("12"),"us-h2":Decimal("7.8")}


def test_first_fit_wastes_flexible_credit_on_early_us_load():
    root=Path(__file__).parents[1]
    data=json.loads((root/"fixtures/carbon/intake.json").read_text())
    table=json.loads((root/"fixtures/tables/lake_sales_orders.json").read_text())
    sizes={f["path"]:f["size_bytes"] for f in table["initial_snapshot"]["files"]}
    result=preview_hourly_offsets(data,sizes)
    assert Decimal(result["uncovered"]["us-h2"])==Decimal("7.8")
    assert Decimal(result["uncovered"]["us-h1"])==0
