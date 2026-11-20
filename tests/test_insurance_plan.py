import hashlib
import json
from pathlib import Path

import pytest

from lakecommit import cli
from lakecommit.insurance_plan import (
    PlanIndeterminateError,
    load_offer_book,
    plan_insurance,
)
from lakecommit.models import FileEntry, Snapshot
from lakecommit.store import SnapshotStore
from test_query_rows import FIXTURES, make_store

OFFERS = Path(__file__).resolve().parents[1] / "fixtures" / "portfolio" / "offers.json"


def fixture_book():
    return load_offer_book(OFFERS)


def book_with(**changes):
    book = fixture_book()
    book.update(changes)
    return book


def plan(tmp_path, book=None, contract="nominal_yield"):
    store, schemas = make_store(tmp_path)
    return plan_insurance(store, FIXTURES, "query-root", schemas, book or fixture_book(), contract)


def make_store_with(tmp_path, v1: bytes, v2: bytes):
    content = tmp_path / "content"
    files, schemas = [], {}
    for name, schema, data in (("v1.jsonl", "cents-v1", v1), ("v2.jsonl", "dollars-v2", v2)):
        rel = f"data/region=us/{name}"
        target = content / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        files.append(FileEntry(path=rel, partition={"region": "us"}, size_bytes=len(data),
                               row_count=len(data.decode("utf-8").splitlines()),
                               content_hash=hashlib.sha256(data).hexdigest()))
        schemas[rel] = schema
    store = SnapshotStore(tmp_path / "store")
    store.initialize(Snapshot(snapshot_id="query-root", parent_snapshot_id=None, sequence=0,
                              logical_time="2025-01-01T00:00:00Z", files=tuple(files)), ("region",))
    return store, schemas, content


def combo(cert, offer_ids):
    for entry in cert["feasible_combinations"] + cert["infeasible_combinations"]:
        if entry["offer_ids"] == offer_ids:
            return entry
    raise AssertionError(f"combination not listed: {offer_ids}")


def scenario(cert, scenario_id):
    return next(s for s in cert["selected"]["scenario_breakdown"] if s["scenario_id"] == scenario_id)


def test_nominal_yield_selects_cover_a(tmp_path):
    cert = plan(tmp_path)
    assert cert["status"] == "purchasable"
    assert cert["selected"]["offer_ids"] == ["cover-A"]
    assert cert["selected"]["premium"] == "3.00"
    assert cert["selected"]["expected_payout"] == "9.50"
    assert cert["selected"]["objective"] == "6.50"
    assert combo(cert, ["cover-BC"])["expected_payout"] == "4.60"
    over = combo(cert, ["cover-A", "cover-BC"])
    assert over["reason"] == "premium_over_budget" and over["premium"] == "7.00"


def test_capital_guard_selects_cover_bc(tmp_path):
    cert = plan(tmp_path, contract="capital_guard")
    assert cert["selected"]["offer_ids"] == ["cover-BC"]
    assert cert["selected"]["premium"] == "4.00"
    joint = scenario(cert, "bc-default")
    assert joint["gross_loss"] == "23.00" and joint["insurer_payout"] == "23.00"
    assert joint["uninsured_loss"] == "0.00"
    assert scenario(cert, "a-default")["uninsured_loss"] == "19.00"
    assert all(float(s["uninsured_loss"]) <= 20.00 for s in cert["selected"]["scenario_breakdown"])


def test_joint_default_is_not_split_into_independent_events(tmp_path):
    joint = plan(tmp_path / "joint", contract="capital_guard")
    assert joint["selected"]["offer_ids"] == ["cover-BC"]
    split = book_with(scenarios=[
        {"scenario_id": "a-default", "probability": "0.50", "defaults": ["d-A"]},
        {"scenario_id": "b-default", "probability": "0.20", "defaults": ["d-B"]},
        {"scenario_id": "c-default", "probability": "0.20", "defaults": ["d-C"]},
        {"scenario_id": "no-default", "probability": "0.10", "defaults": []},
    ])
    cert = plan(tmp_path / "split", split, "capital_guard")
    assert cert["selected"]["offer_ids"] == []  # 11 and 12 each pass the 20.00 limit
    assert cert["selected"]["premium"] == "0.00"


def test_overlapping_offers_never_double_pay(tmp_path):
    book = book_with(
        offers=[
            {"offer_id": "a-1", "debtors": ["d-A"], "premium": "1.00", "deductible": "4.00"},
            {"offer_id": "a-2", "debtors": ["d-A"], "premium": "1.00"},
        ],
        scenarios=[{"scenario_id": "a-default", "probability": "1.00", "defaults": ["d-A"]}],
        premium_budget="5.00",
    )
    cert = plan(tmp_path, book)
    assert combo(cert, ["a-1", "a-2"])["expected_payout"] == "19.00"  # 15.00 + 4.00 remainder
    assert cert["selected"]["offer_ids"] == ["a-2"]  # second overlapping offer adds no payout


def test_partial_coverage_deductible_and_aggregate_limit(tmp_path):
    book = book_with(
        offers=[
            {"offer_id": "partial-a", "debtors": ["d-A"], "premium": "0.50",
             "coverage_fraction": "0.5", "deductible": "1.00", "aggregate_limit": "5.00"},
            {"offer_id": "ded-b", "debtors": ["d-B"], "premium": "0.25", "deductible": "2.50"},
        ],
        scenarios=[
            {"scenario_id": "a-default", "probability": "0.50", "defaults": ["d-A"]},
            {"scenario_id": "b-default", "probability": "0.50", "defaults": ["d-B"]},
        ],
        premium_budget="5.00",
    )
    cert = plan(tmp_path, book)
    assert cert["selected"]["offer_ids"] == ["ded-b", "partial-a"]
    a_loss = scenario(cert, "a-default")
    assert a_loss["insurer_payout"] == "5.00"  # 19*0.5 - 1.00 = 8.50 capped at 5.00
    assert a_loss["uninsured_loss"] == "14.00"
    assert a_loss["claim_allocations"] == [
        {"offer_id": "partial-a", "debtor_id": "d-A", "amount": "5.00"}
    ]
    assert scenario(cert, "b-default")["insurer_payout"] == "8.50"  # 11.00 - 2.50 deductible
    offers = {o["offer_id"]: o for o in cert["offers"]}
    assert offers["partial-a"]["aggregate_limit"] == "5.00"
    assert offers["partial-a"]["deductible"] == "1.00"
    assert offers["ded-b"]["aggregate_limit"] is None


def test_probability_sum_mismatch_rejected(tmp_path):
    book = book_with(scenarios=[
        {"scenario_id": "s1", "probability": "0.60", "defaults": ["d-A"]},
        {"scenario_id": "s2", "probability": "0.30", "defaults": []},
    ])
    with pytest.raises(PlanIndeterminateError) as err:
        plan(tmp_path, book)
    assert err.value.code == "PLAN_PROBABILITY_SUM"


def test_negative_probability_rejected(tmp_path):
    book = book_with(scenarios=[
        {"scenario_id": "s1", "probability": "-0.10", "defaults": ["d-A"]},
        {"scenario_id": "s2", "probability": "1.10", "defaults": []},
    ])
    with pytest.raises(PlanIndeterminateError) as err:
        plan(tmp_path, book)
    assert err.value.code == "PLAN_PROBABILITY_NEGATIVE"


def test_duplicate_offer_id_rejected(tmp_path):
    book = book_with(offers=[
        {"offer_id": "dup", "debtors": ["d-A"], "premium": "1.00"},
        {"offer_id": "dup", "debtors": ["d-B"], "premium": "1.00"},
    ])
    with pytest.raises(PlanIndeterminateError) as err:
        plan(tmp_path, book)
    assert err.value.code == "PLAN_DUPLICATE_OFFER_ID"


def test_offer_with_unknown_debtor_is_not_purchasable(tmp_path):
    book = book_with(offers=fixture_book()["offers"] + [
        {"offer_id": "cover-Z", "debtors": ["d-Z"], "premium": "1.00"},
    ])
    cert = plan(tmp_path, book)
    assert cert["excluded_offers"] == [
        {"offer_id": "cover-Z", "reason": "debtor_not_in_portfolio", "unknown_debtors": ["d-Z"]}
    ]
    assert all("cover-Z" not in c["offer_ids"] for c in cert["feasible_combinations"])


def test_offer_missing_debtors_rejected(tmp_path):
    book = book_with(offers=[{"offer_id": "ghost", "premium": "1.00"}])
    with pytest.raises(PlanIndeterminateError) as err:
        plan(tmp_path, book)
    assert err.value.code == "PLAN_OFFER_DEBTORS_MISSING"


def test_duplicate_invoice_mapping_rejected(tmp_path):
    path = tmp_path / "dup.json"
    path.write_text(
        '{"debtors":{"o-01":"d-A","o-01":"d-B"},"offers":[],"scenarios":[],'
        '"premium_budget":"1.00","authorized_contracts":["nominal_yield"]}',
        encoding="utf-8",
    )
    with pytest.raises(PlanIndeterminateError) as err:
        load_offer_book(path)
    assert err.value.code == "PLAN_DUPLICATE_KEY"


def test_budget_boundary(tmp_path):
    tight = book_with(premium_budget="3.00")
    cert = plan(tmp_path / "nominal", tight)
    assert cert["selected"]["offer_ids"] == ["cover-A"]
    assert combo(cert, ["cover-BC"])["reason"] == "premium_over_budget"
    guard = plan(tmp_path / "guard", tight, "capital_guard")
    assert guard["status"] == "not_purchasable"
    assert guard["selected"] is None
    assert guard["blocking_scenarios"] == [
        {"scenario_id": "bc-default", "defaults": ["d-B", "d-C"],
         "uninsured_loss": "23.00", "capital_loss_limit": "20.00"}
    ]
    exact = plan(tmp_path / "exact", book_with(premium_budget="4.00"), "capital_guard")
    assert exact["selected"]["offer_ids"] == ["cover-BC"]  # premium equal to budget is feasible


def test_snapshot_correction_requires_replan(tmp_path):
    v1 = (FIXTURES / "data/region=us/v1.jsonl").read_bytes()
    v2 = b'{"order_id":"o-03","amount_dollars":"20.00"}\n{"order_id":"o-04","amount_dollars":"12.00"}\n'
    store, schemas, content = make_store_with(tmp_path, v1, v2)
    cert = plan_insurance(store, content, "query-root", schemas, fixture_book(), "capital_guard")
    exposures = {e["debtor_id"]: e["amount"] for e in cert["content_binding"]["exposures"]}
    assert exposures["d-A"] == "29.00"  # corrected order recomputed, not the stale 19.00
    assert cert["status"] == "not_purchasable"
    assert cert["blocking_scenarios"] == [
        {"scenario_id": "bc-default", "defaults": ["d-B", "d-C"],
         "uninsured_loss": "23.00", "capital_loss_limit": "20.00"}
    ]


def test_tampered_file_blocks_plan(tmp_path):
    v1 = (FIXTURES / "data/region=us/v1.jsonl").read_bytes()
    v2 = (FIXTURES / "data/region=us/v2.jsonl").read_bytes()
    store, schemas, content = make_store_with(tmp_path, v1, v2)
    (content / "data/region=us/v1.jsonl").write_bytes(b'{"order_id":"o-01","amount_cents":1}\n')
    with pytest.raises(PlanIndeterminateError) as err:
        plan_insurance(store, content, "query-root", schemas, fixture_book(), "nominal_yield")
    assert err.value.code == "PLAN_SNAPSHOT_BINDING_FAILED"


def test_debtor_mapping_mismatch_blocks_plan(tmp_path):
    book = fixture_book()
    book["debtors"] = {k: v for k, v in book["debtors"].items() if k != "o-01"}
    with pytest.raises(PlanIndeterminateError) as err:
        plan(tmp_path, book)
    assert err.value.code == "PLAN_SNAPSHOT_BINDING_FAILED"


def test_certificate_binds_snapshot_files_and_mapping(tmp_path):
    cert = plan(tmp_path / "a")
    files = {f["path"]: f for f in cert["content_binding"]["files"]}
    for name in ("v1.jsonl", "v2.jsonl"):
        rel = f"data/region=us/{name}"
        assert files[rel]["content_hash"] == hashlib.sha256((FIXTURES / rel).read_bytes()).hexdigest()
    assert cert["content_binding"]["snapshot_id"] == "query-root"
    assert cert["content_binding"]["debtor_mapping"] == fixture_book()["debtors"]
    assert plan(tmp_path / "b")["plan_fingerprint"] == cert["plan_fingerprint"]
    other = plan(tmp_path / "c", contract="capital_guard")
    assert other["plan_fingerprint"] != cert["plan_fingerprint"]


def test_unauthorized_contract_rejected(tmp_path):
    book = book_with(authorized_contracts=["capital_guard"])
    with pytest.raises(PlanIndeterminateError) as err:
        plan(tmp_path, book, "nominal_yield")
    assert err.value.code == "PLAN_CONTRACT_NOT_AUTHORIZED"
    with pytest.raises(PlanIndeterminateError):
        plan(tmp_path / "unknown", fixture_book(), "not_a_contract")


def test_cli_insure(tmp_path, capsys):
    make_store(tmp_path)
    schemas_file = tmp_path / "schemas.json"
    schemas_file.write_text(json.dumps({
        "data/region=us/v1.jsonl": "cents-v1",
        "data/region=us/v2.jsonl": "dollars-v2",
    }), encoding="utf-8")
    code = cli.main([
        "insure", "--workdir", str(tmp_path), "--content-root", str(FIXTURES),
        "--schemas", str(schemas_file), "--offers", str(OFFERS), "--contract", "nominal_yield",
    ])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "purchasable"
    assert out["selected"]["offer_ids"] == ["cover-A"]
