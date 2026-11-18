import copy
import json
from pathlib import Path

import pytest

from lakecommit.errors import SchemaError
from lakecommit.models import FileEntry, Snapshot
from lakecommit.snapshot import manifest_hash
from lakecommit.storage_resilience import main, plan_resilience, preview_scenario_hardening
from lakecommit.store import SnapshotStore

ROOT = Path(__file__).parents[1]
INTAKE = ROOT / "fixtures/storage_resilience/intake.json"
TABLE = ROOT / "fixtures/tables/lake_sales_orders.json"


def fixture_data() -> dict:
    return json.loads(INTAKE.read_text())


def plan(data: dict | None = None, **kwargs) -> dict:
    return plan_resilience(data if data is not None else fixture_data(), **kwargs)


def error_code(data: dict) -> str:
    with pytest.raises(SchemaError) as caught:
        plan_resilience(data)
    return caught.value.code


def make_store(tmp_path, snapshot_id: str, regions: list[str], files_per_region: int = 1) -> SnapshotStore:
    store = SnapshotStore(tmp_path / "work")
    store.reset()
    files = [
        FileEntry(
            path=f"data/region={region}/part-{index:04d}.parquet",
            partition={"region": region},
            size_bytes=10,
            row_count=1,
            content_hash=f"{index}" * 64,
        )
        for region in regions
        for index in range(files_per_region)
    ]
    snapshot = Snapshot(snapshot_id, None, 0, "2024-01-01T00:00:00Z", tuple(files))
    store.initialize(snapshot, ("region",))
    return store


def custom_intake(snapshot_id: str, replicas: dict, scenarios: list[dict],
                  contracts: dict, costs: dict, max_hardened_nodes: int = 1) -> dict:
    return {
        "snapshot_id": snapshot_id,
        "max_hardened_nodes": max_hardened_nodes,
        "table": str(TABLE),
        "region_replicas": replicas,
        "harden_cost": costs,
        "repair_cost": {node: 1 for node in costs},
        "scenarios": scenarios,
        "contracts": contracts,
    }


CONTINUOUS = {"continuous_region": {"allowed_initial_outage_regions": [], "outage_compensation": 0}}
EU_WINDOW = {"eu_repair_window": {"allowed_initial_outage_regions": ["eu"], "outage_compensation": 2}}


def test_committed_snapshot_identity_matches_initial_snapshot():
    table = json.loads(TABLE.read_text())
    snap = table["initial_snapshot"]
    entries = [FileEntry.from_dict(item) for item in snap["files"]]
    result = plan()["snapshot"]
    assert result["snapshot_id"] == snap["snapshot_id"]
    assert result["regions"] == sorted({f["partition"]["region"] for f in snap["files"]})
    assert result["region_row_counts"] == {"ca": 20, "eu": 70, "us": 160}
    assert result["manifest_hash"] == manifest_hash(entries)
    assert result["file_count"] == len(snap["files"]) == 4
    assert result["files"] == [entry.to_dict() | {"region": entry.partition["region"]}
                               for entry in sorted(entries, key=lambda item: item.path)]


def test_legacy_preview_is_clairvoyant_and_cannot_be_one_commitment():
    data = fixture_data()
    assert preview_scenario_hardening(data) == {"storm-west": "A", "storm-east": "C"}
    trap = plan()["preview_trap"]
    assert trap["single_pre_reveal_choice"] is False
    assert trap["distinct_choices"] == ["A", "C"]
    assert trap["summed_per_scenario_hardening_cost"] == 4
    continuous = plan()["plans"]["continuous_region"]
    reasons = {tuple(item["hardening_nodes"]): item
               for item in continuous["optimality"]["hardening_candidates"]}
    assert reasons[("A",)]["feasible"] is False and "eu" in reasons[("A",)]["reason"]
    assert reasons[("C",)]["feasible"] is False and "ca" in reasons[("C",)]["reason"]


def test_continuous_contract_hardens_b_once_at_cost_six():
    plan_data = plan()["plans"]["continuous_region"]
    assert plan_data["hardening"]["nodes"] == ["B"]
    assert plan_data["hardening"]["cost"] == 6
    assert plan_data["costs"]["objective"] == 6
    assert plan_data["optimality"]["minimal_objective"] == 6
    for scenario in plan_data["scenarios"]:
        assert scenario["initial_outage_regions"] == []
        assert scenario["repair"]["node"] is None
        assert scenario["post_repair_outage_regions"] == []
        assert set(scenario["initial"]["regions"]) == {"ca", "eu", "us"}


def test_eu_contract_hardens_a_and_prices_the_outage():
    plan_data = plan()["plans"]["eu_repair_window"]
    assert plan_data["hardening"]["nodes"] == ["A"]
    assert plan_data["hardening"]["cost"] == 2
    assert plan_data["costs"]["objective"] == 5
    assert plan_data["optimality"]["minimal_objective"] == 5
    east = next(item for item in plan_data["scenarios"] if item["scenario_id"] == "storm-east")
    west = next(item for item in plan_data["scenarios"] if item["scenario_id"] == "storm-west")
    assert east["initial_outage_regions"] == ["eu"]
    assert east["repair"] == {"node": "B", "cost": 1, "after_reveal": True, "max_repairs": 1}
    assert east["post_repair_outage_regions"] == []
    assert east["compensation"] == 2 and east["scenario_cost"] == 3
    assert west["initial_outage_regions"] == [] and west["scenario_cost"] == 0


def test_initial_outage_is_not_erased_by_the_repair():
    east = next(item for item in plan()["plans"]["eu_repair_window"]["scenarios"]
                if item["scenario_id"] == "storm-east")
    assert east["outage_timeline"] == {
        "initial_outage_regions": ["eu"],
        "recovered_regions": ["eu"],
        "erased_by_repair": False,
        "note": "repair restores later availability but never removes the initial outage",
    }
    assert east["initial"]["regions"]["eu"]["available"] is False
    assert east["post_repair"]["regions"]["eu"]["available"] is True


def test_eu_contract_never_interrupts_ca_or_us_at_any_phase():
    for scenario in plan()["plans"]["eu_repair_window"]["scenarios"]:
        for phase in ("initial", "post_repair"):
            assert scenario[phase]["regions"]["ca"]["available"] is True
            assert scenario[phase]["regions"]["us"]["available"] is True


def test_repairs_are_after_reveal_and_scenario_local():
    data = plan()["plans"]["eu_repair_window"]
    for scenario in data["scenarios"]:
        repair = scenario["repair"]
        assert repair["after_reveal"] is True and repair["max_repairs"] == 1
        if repair["node"] is not None:
            assert repair["node"] in scenario["failed_nodes"]
            assert repair["node"] not in scenario["initial"]["available_nodes"]
            assert repair["node"] in scenario["post_repair"]["available_nodes"]
    east = next(item for item in data["scenarios"] if item["scenario_id"] == "storm-east")
    west = next(item for item in data["scenarios"] if item["scenario_id"] == "storm-west")
    assert east["repair"]["node"] == "B"
    assert west["repair"]["node"] is None
    assert data["hardening"]["same_for_all_scenarios"] is True
    assert data["hardening"]["before_reveal"] is True


def test_hardened_node_survives_its_failed_scenario():
    result = plan()
    continuous = result["plans"]["continuous_region"]
    assert continuous["hardening"]["nodes"] == ["B"]
    for scenario in continuous["scenarios"]:
        assert "B" in scenario["failed_nodes"]
        assert "B" in scenario["initial"]["available_nodes"]
        assert "B" in scenario["post_repair"]["available_nodes"]


def test_objective_minimizes_hardening_plus_worst_case_scenario_cost():
    data = plan()["plans"]["eu_repair_window"]
    scenario_costs = [item["scenario_cost"] for item in data["scenarios"]]
    assert data["costs"]["objective"] == data["costs"]["hardening"] + max(scenario_costs)
    assert data["costs"]["worst_case_compensation"] == 2
    assert data["costs"]["worst_case_repair"] == 1
    assert data["costs"]["objective"] == 5


def test_repair_tie_break_uses_lowest_node_id(tmp_path):
    store = make_store(tmp_path, "snap-eu", ["eu"])
    data = custom_intake(
        "snap-eu", {"eu": ["B", "C"]},
        [{"scenario_id": "storm", "failed_nodes": ["B", "C"]}],
        EU_WINDOW, {"B": 5, "C": 5},
    )
    result = plan_resilience(data, store=store)
    chosen = result["plans"]["eu_repair_window"]
    assert chosen["hardening"]["nodes"] == []
    assert chosen["scenarios"][0]["repair"]["node"] == "B"
    assert chosen["scenarios"][0]["compensation"] == 2
    assert chosen["costs"]["objective"] == 3


def test_hardening_tie_break_uses_lowest_node_id(tmp_path):
    store = make_store(tmp_path, "snap-ca", ["ca"])
    data = custom_intake(
        "snap-ca", {"ca": ["A", "B"]},
        [{"scenario_id": "storm", "failed_nodes": ["A", "B"]}],
        CONTINUOUS, {"A": 3, "B": 3},
    )
    result = plan_resilience(data, store=store)
    chosen = result["plans"]["continuous_region"]
    assert chosen["hardening"]["nodes"] == ["A"]
    assert chosen["costs"]["objective"] == 3
    assert chosen["optimality"]["tie_break"] == [["A"], [""]]


def test_scenario_order_does_not_change_the_plan():
    data = fixture_data()
    reordered = copy.deepcopy(data)
    reordered["scenarios"].reverse()
    assert plan(reordered) == plan(data)


def test_snapshot_identity_change_recomputes_regions_and_certificate(tmp_path):
    store = make_store(tmp_path, "snap-alt", ["ca", "apac", "us"])
    data = custom_intake(
        "snap-alt", {"ca": ["A", "B"], "apac": ["A", "C"], "us": ["A", "C"]},
        [{"scenario_id": "storm-west", "failed_nodes": ["A", "B"]},
         {"scenario_id": "storm-east", "failed_nodes": ["B", "C"]}],
        CONTINUOUS, {"A": 2, "B": 6, "C": 2},
    )
    result = plan_resilience(data, store=store)
    assert result["snapshot"]["regions"] == ["apac", "ca", "us"]
    assert "eu" not in result["snapshot"]["region_files"]
    certificate = result["plans"]["continuous_region"]["certificate"]
    assert certificate["snapshot_id"] == "snap-alt"
    assert certificate["region_replicas"]["apac"] == ["A", "C"]
    assert "eu" not in certificate["region_replicas"]
    assert certificate["region_files"]["apac"] == ["data/region=apac/part-0000.parquet"]
    assert certificate["fingerprint"] != plan()["plans"]["continuous_region"]["certificate"]["fingerprint"]
    stale = copy.deepcopy(data)
    stale["region_replicas"] = {"ca": ["A", "B"], "eu": ["B", "C"], "us": ["A", "C"]}
    stale["contracts"] = EU_WINDOW
    with pytest.raises(SchemaError) as caught:
        plan_resilience(stale, store=store)
    assert caught.value.code in {"REGION_COVERAGE_MISSING", "REGION_UNKNOWN"}




def test_single_replica_region_can_never_be_treated_as_healthy(tmp_path):
    store = make_store(tmp_path, "snap-thin", ["ca", "eu", "us"])
    data = custom_intake(
        "snap-thin", {"ca": ["A"], "eu": ["B"], "us": ["C"]},
        [{"scenario_id": "storm", "failed_nodes": ["A", "B", "C"]}],
        CONTINUOUS, {"A": 1, "B": 1, "C": 1},
    )
    with pytest.raises(SchemaError) as caught:
        plan_resilience(data, store=store)
    assert caught.value.code == "NO_FEASIBLE_PLAN"
    assert all(not item["feasible"] for item in caught.value.details["candidates"])
    assert all("initial outage regions" in (item["reason"] or "")
               for item in caught.value.details["candidates"])


def test_partition_identity_change_alters_coverage_and_contract_feasibility(tmp_path):
    store = make_store(tmp_path, "snap-files", ["ca", "eu", "us"], files_per_region=2)
    data = custom_intake(
        "snap-files", {"ca": ["A", "B"], "eu": ["B", "C"], "us": ["A", "C"]},
        [{"scenario_id": "storm-west", "failed_nodes": ["A", "B"]},
         {"scenario_id": "storm-east", "failed_nodes": ["B", "C"]}],
        CONTINUOUS, {"A": 2, "B": 6, "C": 2},
    )
    result = plan_resilience(data, store=store)
    assert result["snapshot"]["file_count"] == 6
    assert result["snapshot"]["region_files"]["us"] == [
        "data/region=us/part-0000.parquet", "data/region=us/part-0001.parquet"]
    assert result["plans"]["continuous_region"]["hardening"]["nodes"] == ["B"]
    assert result["plans"]["continuous_region"]["certificate"]["file_count"] == 6
    shifted = copy.deepcopy(data)
    shifted["region_replicas"] = {"ca": ["A", "C"], "eu": ["A", "C"], "us": ["B", "C"]}
    shifted_plan = plan_resilience(shifted, store=store)
    assert shifted_plan["plans"]["continuous_region"]["hardening"]["nodes"] == ["C"]
    assert shifted_plan["plans"]["continuous_region"]["certificate"]["region_replicas"] == {
        "ca": ["A", "C"], "eu": ["A", "C"], "us": ["B", "C"]}


def test_unlisted_contract_is_rejected_while_authorized_pair_is_planned():
    result = plan()
    assert result["authorized_contracts"] == ["continuous_region", "eu_repair_window"]
    data = fixture_data()
    data["contracts"].pop("continuous_region")
    assert plan_resilience(data)["authorized_contracts"] == ["eu_repair_window"]
    data = fixture_data()
    data["contracts"]["continuous_region"]["allowed_initial_outage_regions"] = ["eu"]
    assert error_code(data) == "CONTRACT_INVALID"
    data = fixture_data()
    data["contracts"]["eu_repair_window"]["allowed_initial_outage_regions"] = ["eu", "ca"]
    assert error_code(data) == "CONTRACT_INVALID"
