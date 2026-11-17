"""Solo freight preview for orders read from a verified lake snapshot."""
from __future__ import annotations
import argparse
import json
from decimal import Decimal
from pathlib import Path
from .query_rows import read_snapshot_rows


def solo_quotes(data: dict, verified_rows: list[dict]) -> list[dict]:
    by_order={r["order_id"]:r for r in verified_rows}
    result=[]
    for merchant in data["merchants"]:
        order=by_order[merchant["order_id"]]
        stop=merchant["stop"]
        cost=data["distance"][data["depot"]][stop]+data["distance"][stop][data["depot"]]
        result.append({"merchant_id":merchant["merchant_id"],"order_id":merchant["order_id"],
                       "amount":order["amount"],"solo_cost":cost})
    return result


def verified_solo_quotes(store,content_root:Path,snapshot_id:str,schemas:dict,data:dict)->list[dict]:
    rows=read_snapshot_rows(store,content_root,snapshot_id,schemas,minimum_amount=Decimal("-999999999"))["rows"]
    return solo_quotes(data,rows)


def main(argv=None)->int:
    p=argparse.ArgumentParser()
    p.add_argument("--fixture",type=Path,default=Path("fixtures/lake_freight/intake.json"))
    args=p.parse_args(argv)
    data=json.loads(args.fixture.read_text(encoding="utf-8"))
    print(json.dumps({"solo_route_costs":[data["distance"][data["depot"]][m["stop"]]+
                       data["distance"][m["stop"]][data["depot"]] for m in data["merchants"]]}))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
