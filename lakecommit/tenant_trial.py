"""Observed-only audit of synthetic tenant adoption of a lake data product."""
from __future__ import annotations
from collections import defaultdict

def audit_adoption(events):
    groups={"used":[0,0,0],"unused":[0,0,0]}
    seen=set()
    for row in events:
        if row["tenant_id"] in seen:
            raise ValueError("duplicate tenant")
        seen.add(row["tenant_id"])
        group=groups["used" if row["used_new_view"] else "unused"]
        group[2]+=1
        if row["outcome_observed"]:
            group[1]+=1
            group[0]+=int(row["query_succeeded"])
    return groups
