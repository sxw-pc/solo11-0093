"""Local first-fit offset preview for synthetic lake read loads."""
from __future__ import annotations
import argparse
import json
from decimal import Decimal
from pathlib import Path


def preview_hourly_offsets(data: dict, sizes: dict[str, int]) -> dict:
    remaining={c["certificate_id"]:Decimal(c["units"]) for c in data["certificates"]}
    uncovered={}
    for load in data["loads"]:
        needed=Decimal(sizes[load["file_path"]])/Decimal(100)
        for credit in data["certificates"]:
            if credit["region"]!=load["region"] or load["hour"] not in credit["eligible_hours"]:
                continue
            use=min(needed,remaining[credit["certificate_id"]])
            needed-=use
            remaining[credit["certificate_id"]]-=use
        uncovered[load["load_id"]]=str(needed)
    return {"uncovered":uncovered}


def main(argv=None) -> int:
    p=argparse.ArgumentParser()
    p.add_argument("--fixture",type=Path,default=Path("fixtures/carbon/intake.json"))
    args=p.parse_args(argv)
    data=json.loads(args.fixture.read_text())
    table=json.loads(Path("fixtures/tables/lake_sales_orders.json").read_text())
    sizes={f["path"]:f["size_bytes"] for f in table["initial_snapshot"]["files"]}
    print(json.dumps(preview_hourly_offsets(data,sizes),sort_keys=True))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
