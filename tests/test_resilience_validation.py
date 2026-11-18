"""Intake rejection, cost boundaries and scale limits for resilience planning."""
import copy
import json
from pathlib import Path

import pytest

from lakecommit.errors import SchemaError
from lakecommit.storage_resilience import main, plan_resilience
from test_storage_resilience import fixture_data, make_store

ROOT = Path(__file__).parents[1]
INTAKE = ROOT / "fixtures/storage_resilience/intake.json"


def error_code(data: dict) -> str:
    with pytest.raises(SchemaError) as caught:
        plan_resilience(data)
    return caught.value.code


def test_uncommitted_or_mismatched_snapshot_is_rejected(tmp_path):
    store = make_store(tmp_path, "snap-0000", ["ca", "eu", "us"])
    data = fixture_data()
    data["snapshot_id"] = "snap-9999"
    with pytest.raises(SchemaError) as caught:
        plan_resilience(data, store=store)
    assert caught.value.code == "SNAPSHOT_NOT_COMMITTED"
    assert caught.value.details["snapshot_id"] == "snap-9999"
    assert error_code(data) == "SNAPSHOT_NOT_COMMITTED"


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda d: d["region_replicas"]["ca"].append("Z"), "NODE_UNKNOWN"),
        (lambda d: d["scenarios"][0]["failed_nodes"].append("Z"), "NODE_UNKNOWN"),
        (lambda d: d["scenarios"].append(copy.deepcopy(d["scenarios"][0])), "SCENARIO_ID_DUPLICATE"),
        (lambda d: d["region_replicas"].__setitem__("mars", ["A"]), "REGION_UNKNOWN"),
        (lambda d: d["region_replicas"].__setitem__("ca", []), "REGION_REPLICA_MISSING"),
        (lambda d: d["region_replicas"].__setitem__("ca", ["A", "A"]), "REGION_REPLICA_DUPLICATE"),
        (lambda d: d["contracts"]["eu_repair_window"].__setitem__(
            "allowed_initial_outage_regions", ["mars"]), "CONTRACT_REGION_UNKNOWN"),
        (lambda d: d["contracts"].__setitem__("free_lunch", {"allowed_initial_outage_regions": [],
                                                             "outage_compensation": 0}), "CONTRACT_UNKNOWN"),
        (lambda d: d.__setitem__("max_hardened_nodes", 3), "HARDENING_BOUND_INVALID"),
        (lambda d: d.pop("region_replicas"), "REGION_REPLICAS_REQUIRED"),
    ],
)
def test_invalid_intake_is_rejected(mutate, code):
    data = fixture_data()
    mutate(data)
    assert error_code(data) == code


@pytest.mark.parametrize(
    "bad_cost", [-1, True, 1.5, "2", None]
)
def test_cost_boundaries_are_rejected(bad_cost):
    data = fixture_data()
    data["harden_cost"]["A"] = bad_cost
    assert error_code(data) in {"COST_NEGATIVE", "COST_INVALID"}
    data = fixture_data()
    data["repair_cost"]["A"] = bad_cost
    assert error_code(data) in {"COST_NEGATIVE", "COST_INVALID"}


def test_missing_cost_table_and_mismatched_nodes_are_rejected():
    data = fixture_data()
    data.pop("harden_cost")
    assert error_code(data) == "COST_TABLE_REQUIRED"
    data = fixture_data()
    data["repair_cost"]["D"] = 1
    assert error_code(data) == "COST_TABLE_MISMATCH"


def test_scale_limits_are_enforced():
    data = fixture_data()
    for node in ["D", "E", "F", "G", "H", "I"]:
        data["harden_cost"][node] = 1
        data["repair_cost"][node] = 1
    assert error_code(data) == "SCALE_LIMIT_EXCEEDED"
    data = fixture_data()
    for index in range(3):
        data["scenarios"].append({"scenario_id": f"extra-{index}", "failed_nodes": []})
    assert error_code(data) == "SCALE_LIMIT_EXCEEDED"


def test_no_feasible_plan_is_rejected_not_reported_healthy():
    data = fixture_data()
    data["scenarios"] = [{"scenario_id": "total", "failed_nodes": ["A", "B", "C"]}]
    assert error_code(data) == "NO_FEASIBLE_PLAN"


def test_cli_emits_plan_and_rejects_bad_input(tmp_path, capsys):
    assert main(["--fixture", str(INTAKE)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["plans"]["continuous_region"]["hardening"]["nodes"] == ["B"]
    bad = fixture_data()
    bad["harden_cost"]["A"] = -2
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad))
    assert main(["--fixture", str(path)]) == 3
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "COST_NEGATIVE"


def test_zero_costs_and_missing_snapshot_field_boundaries():
    data = fixture_data()
    data["harden_cost"]["A"] = 0
    data["repair_cost"]["A"] = 0
    assert plan_resilience(data)["plans"]["continuous_region"]["hardening"]["nodes"] == ["B"]
    data = fixture_data()
    data.pop("snapshot_id")
    with pytest.raises(SchemaError) as caught:
        plan_resilience(data)
    assert caught.value.code == "SNAPSHOT_ID_INVALID"
    data = fixture_data()
    data["scenarios"] = []
    assert error_code(data) == "SCENARIO_REQUIRED"
    data = fixture_data()
    data["contracts"] = {}
    assert error_code(data) == "CONTRACT_REQUIRED"
