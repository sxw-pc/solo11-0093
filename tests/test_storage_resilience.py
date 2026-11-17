import json
from pathlib import Path
from lakecommit.storage_resilience import preview_scenario_hardening


def test_replica_regions_match_initial_snapshot():
    root=Path(__file__).parents[1]
    data=json.loads((root/"fixtures/storage_resilience/intake.json").read_text())
    table=json.loads((root/"fixtures/tables/lake_sales_orders.json").read_text())
    snap=table["initial_snapshot"]
    assert data["snapshot_id"]==snap["snapshot_id"]
    assert set(data["region_replicas"])=={f["partition"]["region"] for f in snap["files"]}
    assert {r:sum(f["row_count"] for f in snap["files"] if f["partition"]["region"]==r) for r in data["region_replicas"]}=={"ca":20,"eu":70,"us":160}


def test_preview_uses_incompatible_scenario_specific_first_decisions():
    root=Path(__file__).parents[1]
    data=json.loads((root/"fixtures/storage_resilience/intake.json").read_text())
    assert preview_scenario_hardening(data)=={"storm-west":"A","storm-east":"C"}
    assert data["harden_cost"]["B"]>data["harden_cost"]["A"]
