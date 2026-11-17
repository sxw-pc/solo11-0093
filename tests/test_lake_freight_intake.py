import json
from pathlib import Path
from lakecommit.freight_intake import verified_solo_quotes
from test_query_rows import make_store,FIXTURES


def test_solo_freight_uses_verified_snapshot_amounts(tmp_path):
    store,schemas=make_store(tmp_path)
    root=Path(__file__).parents[1]
    data=json.loads((root/"fixtures/lake_freight/intake.json").read_text())
    quotes=verified_solo_quotes(store,FIXTURES,"query-root",schemas,data)
    assert [q["order_id"] for q in quotes]==["o-01","o-02","o-03"]
    assert [q["amount"] for q in quotes]==["9","11","10.00"]
    assert [q["solo_cost"] for q in quotes]==[20,18,16]
    assert sum(q["solo_cost"] for q in quotes)==54


def test_route_and_contract_fixture_has_distinct_billing_bases():
    root=Path(__file__).parents[1]
    data=json.loads((root/"fixtures/lake_freight/intake.json").read_text())
    assert data["vehicle_capacity"]==2
    assert {m["merchant_id"] for m in data["merchants"]}=={"m-A","m-B","m-C"}
    assert set(data["contracts"])=={"value_share","marginal_share"}
