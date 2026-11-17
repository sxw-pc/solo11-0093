import json
from pathlib import Path
from decimal import Decimal
from lakecommit.tenant_trial import audit_adoption
from lakecommit.tenant_trial_design import potential_use,potential_success
from lakecommit.query_rows import read_snapshot_rows
from test_query_rows import make_store,FIXTURES

def test_synthetic_tenant_trial(tmp_path):
    root=Path(__file__).parents[1]/"fixtures/tenant_trial"
    data=json.loads((root/"adoption.json").read_text())
    events=data["events"]
    store,schemas=make_store(tmp_path)
    assert read_snapshot_rows(store,FIXTURES,"query-root",schemas,minimum_amount=Decimal("0"))["rows"]
    summary=json.loads((root/"summary.json").read_text())
    assert len(events)==120
    for offered,key in ((0,"not_offered"),(1,"offered")):
        rows=[r for r in events if r["offered_new_view"]==offered]
        expected=summary[key]
        assert len(rows)==expected["total"]
        assert sum(r["used_new_view"] for r in rows)==expected["used"]
        assert sum(r["outcome_observed"] for r in rows)==expected["observed"]
        assert sum(int(r["query_succeeded"] or 0) for r in rows)==expected["success_observed"]
    for r in events:
        i=int(r["tenant_id"].split("-")[1])
        assert potential_use(i,1)>=potential_use(i,0)
        assert r["used_new_view"]==potential_use(i,r["offered_new_view"])
        if r["outcome_observed"]:
            assert r["query_succeeded"]==potential_success(i,r["used_new_view"])
    assert sum(v[2] for v in audit_adoption(events).values())==120
