import json
from pathlib import Path
from lakecommit.portfolio_intake import debtor_exposures
from test_query_rows import make_store,FIXTURES

def test_portfolio_fixture_exposures(tmp_path):
    store,schemas=make_store(tmp_path)
    f=json.loads((Path(__file__).parents[1]/"fixtures/portfolio/offers.json").read_text())
    result=debtor_exposures(store,FIXTURES,"query-root",schemas,f["debtors"])
    assert result["exposures"]==[{"debtor_id":"d-A","amount":"19.00"},
                                 {"debtor_id":"d-B","amount":"11.00"},
                                 {"debtor_id":"d-C","amount":"12.00"}]
