"""Fictional period-tax preview from verified synthetic lake order rows."""
from __future__ import annotations
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from .query_rows import read_snapshot_rows

def preview_tax(store, content_root: Path, snapshot_id: str, schemas: dict, terms: dict, rates: dict):
    data=read_snapshot_rows(store,content_root,snapshot_id,schemas,minimum_amount=Decimal("-999999999"))
    result=[]
    for row in data["rows"]:
        order_id=row["order_id"]
        term=terms[order_id]
        amount=Decimal(row["amount"])
        rate=Decimal(str(rates[term["jurisdiction"]][term["category"]]))
        tax=(amount*rate).quantize(Decimal("0.01"),rounding=ROUND_HALF_UP)
        result.append({"order_id":order_id,"jurisdiction":term["jurisdiction"],
                       "channel":term["channel"],"amount":str(amount),"tax":str(tax),
                       "source":row["source"]})
    return {"snapshot_id":snapshot_id,"rows":result}
