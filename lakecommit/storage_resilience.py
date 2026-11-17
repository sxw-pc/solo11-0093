"""Clairvoyant per-scenario hardening preview for synthetic snapshot replicas."""
from __future__ import annotations
import argparse
import json
from pathlib import Path


def preview_scenario_hardening(data: dict) -> dict[str, str]:
    choices={}
    for scenario in data["scenarios"]:
        failures=set(scenario["failed_nodes"])
        feasible=[]
        for node,cost in data["harden_cost"].items():
            available=set(data["harden_cost"])-(failures-{node})
            if all(set(replicas)&available for replicas in data["region_replicas"].values()):
                feasible.append((cost,node))
        choices[scenario["scenario_id"]]=min(feasible)[1] if feasible else "none"
    return choices


def main(argv=None) -> int:
    p=argparse.ArgumentParser()
    p.add_argument("--fixture",type=Path,default=Path("fixtures/storage_resilience/intake.json"))
    args=p.parse_args(argv)
    print(json.dumps(preview_scenario_hardening(json.loads(args.fixture.read_text())),sort_keys=True))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
