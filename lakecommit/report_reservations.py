"""Local slot-first preview for synthetic lake report requests."""
from __future__ import annotations
import argparse
import json
from pathlib import Path


def preview_reservations(data: dict, contract_name: str) -> dict:
    """Place each request independently; each request is charged all its files."""
    eligible=data["contracts"][contract_name]["eligible_slots"]
    remaining={int(k):v for k,v in data["slot_capacity"].items()}
    placed={}
    for req in sorted(data["requests"], key=lambda r: (-r["value"],r["request_id"])):
        for slot in eligible[req["request_id"]]:
            if remaining[slot]>=len(req["file_paths"]):
                remaining[slot]-=len(req["file_paths"])
                placed[req["request_id"]]=slot
                break
    return {"placed":placed,"remaining":remaining}


def main(argv=None) -> int:
    p=argparse.ArgumentParser()
    p.add_argument("--fixture",type=Path,default=Path("fixtures/report_reservations/intake.json"))
    p.add_argument("--contract",choices=("certified_close","exploratory_quick"),default="certified_close")
    args=p.parse_args(argv)
    print(json.dumps(preview_reservations(json.loads(args.fixture.read_text()),args.contract),sort_keys=True))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
