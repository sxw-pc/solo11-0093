"""Synthetic debtor exposure intake from a verified committed snapshot."""
from __future__ import annotations
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from .invoice_read import preview_invoices

def debtor_exposures(store, content_root: Path, snapshot_id: str, schemas: dict, debtors: dict):
    invoices=preview_invoices(store,content_root,snapshot_id,schemas)["invoices"]
    totals=defaultdict(Decimal)
    for invoice in invoices:
        totals[debtors[invoice["order_id"]]]+=Decimal(invoice["amount"])
    return {"snapshot_id":snapshot_id,"exposures":
            [{"debtor_id":k,"amount":str(v.quantize(Decimal("0.01")))} for k,v in sorted(totals.items())]}
