"""Single-region preview of fictional lake data license bids."""
from __future__ import annotations
import argparse
import json
from pathlib import Path


def preview_single_region(data: dict) -> list[str]:
    free=set(data["regions"])
    winners=[]
    for bid in sorted(data["bids"],key=lambda x:(-x["value"],x["bid_id"])):
        if len(bid["regions"])!=1: continue
        region=bid["regions"][0]
        if region in free:
            free.remove(region)
            winners.append(bid["bid_id"])
    return winners


def main(argv=None):
    p=argparse.ArgumentParser()
    p.add_argument("--fixture",type=Path,default=Path("fixtures/license_market/intake.json"))
    args=p.parse_args(argv)
    print(json.dumps(preview_single_region(json.loads(args.fixture.read_text()))))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
