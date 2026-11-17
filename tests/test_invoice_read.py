from lakecommit.invoice_read import preview_invoices
from test_query_rows import make_store, FIXTURES

def test_invoice_preview_from_verified_snapshot(tmp_path):
    store, schemas = make_store(tmp_path)
    result = preview_invoices(store, FIXTURES, "query-root", schemas)
    assert result["snapshot_id"] == "query-root"
    assert len(result["invoices"]) == 4
    assert [row["order_id"] for row in result["invoices"]] == ["o-01","o-02","o-03","o-04"]
