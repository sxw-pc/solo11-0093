"""Bounded two-phase availability search and contract plan selection."""
from __future__ import annotations

from itertools import combinations

from lakecommit.errors import SchemaError
from lakecommit.fingerprint import object_fingerprint
from lakecommit.resilience_input import MAX_REPAIRED_NODES


def _node_name(nodes: tuple[str, ...]) -> str | None:
    return nodes[0] if len(nodes) == 1 else None


def _region_state(regions: list[str], replicas: dict, available: set[str]) -> tuple[dict, list[str]]:
    state = {}
    for region in regions:
        live = sorted(set(replicas[region]) & available)
        state[region] = {"available": bool(live), "available_nodes": live,
                         "replicas": list(replicas[region])}
    return state, [region for region in regions if not state[region]["available"]]


def _scenario_plan(scenario: dict, hardened: tuple[str, ...], contract: dict, spec: dict):
    nodes = set(spec["nodes"])
    failed = set(scenario["failed_nodes"])
    initial_available = (nodes - failed) | set(hardened)
    regions = sorted(spec["region_replicas"])
    initial_state, initial_outage = _region_state(
        regions, spec["region_replicas"], initial_available)
    if not set(initial_outage) <= set(contract["allowed_initial_outage_regions"]):
        return None, "initial outage regions %s are not permitted" % initial_outage
    best = None
    for repair_node in [None] + sorted(failed - initial_available):
        post_available = set(initial_available)
        if repair_node is not None:
            post_available.add(repair_node)
        post_state, post_outage = _region_state(regions, spec["region_replicas"], post_available)
        if post_outage:
            continue
        repair_cost = spec["costs"][repair_node]["repair"] if repair_node else 0
        compensation = contract["outage_compensation"] if initial_outage else 0
        key = (compensation + repair_cost, "" if repair_node is None else repair_node)
        if best is None or key < best[0]:
            best = (key, repair_node, post_available, post_state, post_outage,
                    repair_cost, compensation)
    if best is None:
        return None, "no single repair restores every region"
    _, repair_node, post_available, post_state, post_outage, repair_cost, compensation = best
    return {
        "scenario_id": scenario["scenario_id"],
        "failed_nodes": list(scenario["failed_nodes"]),
        "initial": {"available_nodes": sorted(initial_available),
                    "regions": initial_state, "outage_regions": initial_outage},
        "initial_outage_regions": initial_outage,
        "repair": {"node": repair_node, "cost": repair_cost,
                   "after_reveal": True, "max_repairs": MAX_REPAIRED_NODES},
        "post_repair": {"available_nodes": sorted(post_available),
                        "regions": post_state, "outage_regions": post_outage},
        "post_repair_outage_regions": post_outage,
        "outage_timeline": {
            "initial_outage_regions": initial_outage,
            "recovered_regions": sorted(set(initial_outage) - set(post_outage)),
            "erased_by_repair": False,
            "note": "repair restores later availability but never removes the initial outage",
        },
        "compensation": compensation,
        "scenario_cost": compensation + repair_cost,
    }, None


def evaluate_plan(spec: dict, contract_name: str, contract: dict, hardened: tuple[str, ...]):
    scenario_plans = []
    for scenario in spec["scenarios"]:
        result, reason = _scenario_plan(scenario, hardened, contract, spec)
        if result is None:
            return None, "%s: %s" % (scenario["scenario_id"], reason)
        scenario_plans.append(result)
    harden_cost = sum(spec["costs"][node]["harden"] for node in hardened)
    worst = max((item["scenario_cost"] for item in scenario_plans), default=0)
    return {
        "contract": contract_name,
        "contract_terms": dict(contract),
        "hardening": {"node": _node_name(hardened), "nodes": list(hardened),
                      "cost": harden_cost, "before_reveal": True,
                      "same_for_all_scenarios": True,
                      "max_hardened_nodes": spec["max_hardened_nodes"]},
        "scenarios": scenario_plans,
        "costs": {
            "hardening": harden_cost,
            "worst_case_compensation": max((item["compensation"] for item in scenario_plans), default=0),
            "worst_case_repair": max((item["repair"]["cost"] for item in scenario_plans), default=0),
            "worst_case_scenario_cost": worst,
            "objective": harden_cost + worst,
        },
    }, None


def _certificate(plan: dict, identity: dict, spec: dict) -> dict:
    body = {
        "snapshot_id": identity["snapshot_id"],
        "manifest_hash": identity["manifest_hash"],
        "commit_hash": identity["commit_hash"],
        "file_count": identity["file_count"],
        "region_files": {region: list(identity["region_files"][region])
                         for region in identity["regions"]},
        "region_replicas": {region: list(spec["region_replicas"][region])
                            for region in identity["regions"]},
        "contract": plan["contract"],
        "hardening": {"nodes": list(plan["hardening"]["nodes"]),
                      "cost": plan["hardening"]["cost"]},
        "scenarios": [{"scenario_id": item["scenario_id"],
                       "failed_nodes": item["failed_nodes"],
                       "initial_outage_regions": item["initial_outage_regions"],
                       "repair_node": item["repair"]["node"],
                       "post_repair_outage_regions": item["post_repair_outage_regions"],
                       "compensation": item["compensation"],
                       "scenario_cost": item["scenario_cost"]}
                      for item in plan["scenarios"]],
        "objective": plan["costs"]["objective"],
    }
    return dict(body, fingerprint=object_fingerprint(body))


def _tie_break(spec: dict, contract_name: str, contract: dict, item: dict):
    plan, _ = evaluate_plan(spec, contract_name, contract, tuple(item["hardening_nodes"]))
    repairs = tuple("" if value["repair"]["node"] is None else value["repair"]["node"]
                    for value in plan["scenarios"])
    return (item["objective"], tuple(item["hardening_nodes"]), repairs)


def _hardening_candidates(spec: dict) -> list[tuple[str, ...]]:
    nodes = spec["nodes"]
    limit = spec["max_hardened_nodes"]
    return [combo for size in range(limit + 1) for combo in combinations(nodes, size)]


def plan_contract(spec: dict, identity: dict, name: str, contract: dict) -> dict:
    candidates = []
    for hardened in _hardening_candidates(spec):
        plan, reason = evaluate_plan(spec, name, contract, hardened)
        candidates.append({
            "hardening_nodes": list(hardened),
            "harden_cost": sum(spec["costs"][node]["harden"] for node in hardened),
            "feasible": plan is not None,
            "reason": reason,
            "objective": plan["costs"]["objective"] if plan else None,
            "scenario_repairs": [item["repair"]["node"] for item in plan["scenarios"]]
            if plan else None,
        })
    feasible = [item for item in candidates if item["feasible"]]
    if not feasible:
        raise SchemaError("NO_FEASIBLE_PLAN", "no pre-reveal hardening decision satisfies the contract",
                          field_path=f"contracts.{name}",
                          details={"contract": name, "candidates": candidates})
    selected = min(feasible, key=lambda item: _tie_break(spec, name, contract, item))
    plan, _ = evaluate_plan(spec, name, contract, tuple(selected["hardening_nodes"]))
    plan["optimality"] = {
        "rule": "minimize hardening cost + worst-case (compensation + repair), then initial node_ids, then per-scenario repair node_id",
        "hardening_candidates": candidates,
        "feasible_plans": [{"hardening_nodes": item["hardening_nodes"],
                            "objective": item["objective"],
                            "scenario_repairs": item["scenario_repairs"]} for item in feasible],
        "selected_objective": selected["objective"],
        "minimal_objective": min(item["objective"] for item in feasible),
        "minimal": True,
        "tie_break": [list(selected["hardening_nodes"]),
                      ["" if value is None else value for value in selected["scenario_repairs"]]],
        "repair_phase": "after scenario reveal, at most one failed node per scenario",
    }
    plan["certificate"] = _certificate(plan, identity, spec)
    return plan
