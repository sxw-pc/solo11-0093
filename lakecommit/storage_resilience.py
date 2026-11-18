"""Contract-aware hardening and repair planning for synthetic snapshot replicas.

The legacy :func:`preview_scenario_hardening` helper is kept only to exhibit the
clairvoyant trap: it picks a different cheapest node per scenario, so its choices
cannot be combined into one pre-reveal hardening commitment.  Use
:func:`plan_resilience` for the audited two-phase plan.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from lakecommit.errors import InputError, LakeCommitError
from lakecommit.fingerprint import pretty_json
from lakecommit.resilience_input import (
    MAX_NODES,
    MAX_REGIONS,
    MAX_REPAIRED_NODES,
    MAX_SCENARIOS,
    load_intake,
)
from lakecommit.resilience_planner import plan_contract
from lakecommit.store import SnapshotStore


def preview_scenario_hardening(data: dict) -> dict[str, str]:
    """Legacy clairvoyant preview (unsafe as a pre-reveal commitment)."""
    choices = {}
    for scenario in data["scenarios"]:
        failures = set(scenario["failed_nodes"])
        feasible = []
        for node, cost in data["harden_cost"].items():
            available = set(data["harden_cost"]) - (failures - {node})
            if all(set(replicas) & available for replicas in data["region_replicas"].values()):
                feasible.append((cost, node))
        choices[scenario["scenario_id"]] = min(feasible)[1] if feasible else "none"
    return choices


def plan_resilience(data: dict, *, store: SnapshotStore | None = None,
                    base_dir: Path | None = None) -> dict:
    """Build contract-specific two-phase plans for one committed snapshot."""
    identity, spec = load_intake(data, store=store, base_dir=base_dir)
    plans = {name: plan_contract(spec, identity, name, terms)
             for name, terms in spec["contracts"].items()}
    preview = preview_scenario_hardening(data)
    distinct = sorted(set(preview.values()))
    costs = spec["costs"]
    return {
        "snapshot": identity,
        "nodes": spec["nodes"],
        "limits": {"max_nodes": MAX_NODES, "max_regions": MAX_REGIONS,
                   "max_scenarios": MAX_SCENARIOS,
                   "max_hardened_nodes": spec["max_hardened_nodes"],
                   "max_repaired_nodes_per_scenario": MAX_REPAIRED_NODES},
        "region_replicas": spec["region_replicas"],
        "scenario_order": [item["scenario_id"] for item in spec["scenarios"]],
        "authorized_contracts": sorted(spec["contracts"]),
        "preview_trap": {
            "scenario_specific_choices": preview,
            "distinct_choices": distinct,
            "single_pre_reveal_choice": False if len(distinct) > 1 else (
                distinct[0] if distinct else None),
            "summed_per_scenario_hardening_cost": sum(
                costs[node]["harden"] for node in distinct if node in costs),
            "reason": "per-scenario cheapest choices use future information and cannot be combined into one pre-reveal commitment",
        },
        "plans": plans,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path,
                        default=Path("fixtures/storage_resilience/intake.json"))
    parser.add_argument("--workdir", type=Path, default=None,
                        help="optional committed SnapshotStore workdir")
    args = parser.parse_args(argv)
    try:
        data = json.loads(args.fixture.read_text(encoding="utf-8"))
        store = SnapshotStore(args.workdir) if args.workdir else None
        result = plan_resilience(data, store=store, base_dir=args.fixture.parent)
    except LakeCommitError as exc:
        sys.stderr.write(pretty_json(exc.as_dict()))
        return exc.exit_code
    except (OSError, json.JSONDecodeError) as exc:
        error = InputError("INTAKE_READ_ERROR", "cannot read intake fixture",
                           location=str(args.fixture), details={"type": exc.__class__.__name__})
        sys.stderr.write(pretty_json(error.as_dict()))
        return error.exit_code
    sys.stdout.write(pretty_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
