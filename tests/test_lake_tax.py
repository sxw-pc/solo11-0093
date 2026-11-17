import json
from pathlib import Path
from lakecommit.tax_preview import preview_tax
from test_query_rows import make_store,FIXTURES

def test_lake_tax_preview_is_from_verified_rows(tmp_path):
    store,schemas=make_store(tmp_path)
    data=json.loads((Path(__file__).parents[1]/"fixtures/lake_tax/period.json").read_text())
    result=preview_tax(store,FIXTURES,"query-root",schemas,data["terms"],data["rates"])
    assert [r["tax"] for r in result["rows"]]==["0.90","1.10","0.80","0.24"]
