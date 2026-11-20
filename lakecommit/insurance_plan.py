"""Bounded offline credit-insurance purchase planner bound to a verified snapshot.

The planner sits on top of the verified read chain: ``query_rows`` checks every
committed file (size, hash, row count), ``invoice_read`` aggregates orders and
``portfolio_intake`` maps them to debtors.  Every certificate is therefore bound
to the snapshot id, the committed file hashes and the debtor mapping; a corrected
or extended snapshot must be planned again from scratch.  All data is synthetic
and never used for real credit decisions.
"""

from __future__ import annotations

import itertools
from decimal import Decimal
from pathlib import Path

from lakecommit.fingerprint import object_fingerprint
from lakecommit.insurance_book import (
    PlanIndeterminateError,
    _reject,
    decimal_field,
    debtor_map,
    load_offer_book,
    money,
    validate_offers,
    validate_scenarios,
)
from lakecommit.portfolio_intake import debtor_exposures

CONTRACTS = ("nominal_yield", "capital_guard")
PLAN_SCHEMA = "lakecommit.insurance_plan/v1"

__all__ = [
    "CONTRACTS",
    "PLAN_SCHEMA",
    "PlanIndeterminateError",
    "load_offer_book",
    "plan_insurance",
]


def _claims(defaults: list, exposures: dict, offers: list) -> tuple:
    """Price one joint default scenario; a debtor's loss is paid at most once."""
    gross = sum((exposures[d] for d in defaults), Decimal(0))
    unpaid = {d: exposures[d] for d in defaults}
    limit_left = {o["offer_id"]: o["aggregate_limit"] for o in offers if o["aggregate_limit"] is not None}
    allocations = []
    for debtor in sorted(defaults):
        for offer in offers:
            if debtor not in offer["debtors"] or unpaid[debtor] <= 0:
                continue
            eligible = min(unpaid[debtor], exposures[debtor] * offer["coverage_fraction"])
            payout = max(Decimal(0), eligible - offer["deductible"])
            if offer["offer_id"] in limit_left:
                payout = min(payout, limit_left[offer["offer_id"]])
            if payout <= 0:
                continue
            allocations.append({"offer_id": offer["offer_id"], "debtor_id": debtor, "amount": payout})
            unpaid[debtor] -= payout
            if offer["offer_id"] in limit_left:
                limit_left[offer["offer_id"]] -= payout
    paid = sum((item["amount"] for item in allocations), Decimal(0))
    return gross, paid, allocations


def _evaluate(combo: tuple, scenarios: list, exposures: dict) -> dict:
    premium = sum((offer["premium"] for offer in combo), Decimal(0))
    expected = Decimal(0)
    worst = Decimal(0)
    breakdown = []
    for scenario in scenarios:
        gross, paid, allocations = _claims(scenario["defaults"], exposures, list(combo))
        uninsured = gross - paid
        expected += scenario["probability"] * paid
        worst = max(worst, uninsured)
        breakdown.append(
            {
                "scenario_id": scenario["scenario_id"],
                "probability": str(scenario["probability"]),
                "defaults": scenario["defaults"],
                "gross_loss": money(gross),
                "insurer_payout": money(paid),
                "uninsured_loss": money(uninsured),
                "net_loss": money(uninsured + premium),
                "claim_allocations": [{**item, "amount": money(item["amount"])} for item in allocations],
            }
        )
    return {
        "offer_ids": [offer["offer_id"] for offer in combo],
        "premium": premium,
        "expected_payout": expected,
        "worst_uninsured": worst,
        "breakdown": breakdown,
    }


def _public_offer(offer: dict) -> dict:
    return {
        "offer_id": offer["offer_id"],
        "debtors": offer["debtors"],
        "premium": money(offer["premium"]),
        "coverage_fraction": str(offer["coverage_fraction"]),
        "deductible": money(offer["deductible"]),
        "aggregate_limit": money(offer["aggregate_limit"]) if offer["aggregate_limit"] is not None else None,
    }


def _select(contract: str, feasible: list, limit: Decimal | None) -> tuple:
    if contract == "nominal_yield":
        chosen = min(feasible, key=lambda c: (c["premium"] - c["expected_payout"], tuple(c["offer_ids"])))
        return chosen, []
    candidates = [c for c in feasible if c["worst_uninsured"] <= limit]
    if candidates:
        return min(candidates, key=lambda c: (c["premium"], tuple(c["offer_ids"]))), []

    def violations(combo: dict) -> list:
        return [b for b in combo["breakdown"] if Decimal(b["uninsured_loss"]) > limit]

    best = min(feasible, key=lambda c: (len(violations(c)), c["premium"], tuple(c["offer_ids"])))
    blocking = [
        {
            "scenario_id": item["scenario_id"],
            "defaults": item["defaults"],
            "uninsured_loss": item["uninsured_loss"],
            "capital_loss_limit": money(limit),
        }
        for item in violations(best)
    ]
    return None, blocking


def plan_insurance(store, content_root, snapshot_id: str, schemas: dict, offer_book: dict, contract: str) -> dict:
    """Evaluate an offer book against a verified snapshot and sign a purchase certificate."""
    if not isinstance(offer_book, dict):
        _reject("PLAN_OFFER_BOOK_INVALID", "offer book must be a JSON object")
    authorized = offer_book.get("authorized_contracts", [])
    if contract not in CONTRACTS or not isinstance(authorized, list) or contract not in authorized:
        _reject("PLAN_CONTRACT_NOT_AUTHORIZED", "contract is not authorized for this offer book", contract=contract)
    budget = decimal_field(offer_book.get("premium_budget"), "premium_budget")
    if budget < 0:
        _reject("PLAN_BUDGET_NEGATIVE", "premium_budget must not be negative")
    limit = None
    if contract == "capital_guard":
        limit = decimal_field(offer_book.get("capital_loss_limit"), "capital_loss_limit")
        if limit < 0:
            _reject("PLAN_CAPITAL_LIMIT_INVALID", "capital_loss_limit must not be negative")
    mapping = debtor_map(offer_book)
    try:
        exposure_rows = debtor_exposures(store, Path(content_root), snapshot_id, schemas, mapping)["exposures"]
    except (KeyError, ValueError) as exc:
        raise PlanIndeterminateError(
            "PLAN_SNAPSHOT_BINDING_FAILED",
            "snapshot content or debtor mapping failed verification; no plan was made",
            details={"reason": str(exc)},
        ) from exc
    exposures = {row["debtor_id"]: Decimal(row["amount"]) for row in exposure_rows}
    offers, excluded = validate_offers(offer_book, set(exposures))
    scenarios = validate_scenarios(offer_book, set(exposures))
    combos = [
        _evaluate(combo, scenarios, exposures)
        for size in range(len(offers) + 1)
        for combo in itertools.combinations(offers, size)
    ]
    feasible = [combo for combo in combos if combo["premium"] <= budget]
    infeasible = [
        {
            "offer_ids": combo["offer_ids"],
            "premium": money(combo["premium"]),
            "reason": "premium_over_budget",
            "premium_budget": money(budget),
        }
        for combo in combos
        if combo["premium"] > budget
    ]
    chosen, blocking = _select(contract, feasible, limit)
    snapshot = store.load_snapshot(snapshot_id)
    files = [
        {
            "path": entry.path,
            "content_hash": entry.content_hash,
            "size_bytes": entry.size_bytes,
            "row_count": entry.row_count,
        }
        for entry in sorted(snapshot.files, key=lambda item: item.path)
    ]
    status = "purchasable" if chosen is not None else "not_purchasable"
    selected = None
    if chosen is not None:
        selected = {
            "offer_ids": chosen["offer_ids"],
            "premium": money(chosen["premium"]),
            "expected_payout": money(chosen["expected_payout"]),
            "objective": money(chosen["expected_payout"] - chosen["premium"]),
            "worst_uninsured_loss": money(chosen["worst_uninsured"]),
            "scenario_breakdown": chosen["breakdown"],
        }
    certificate = {
        "schema": PLAN_SCHEMA,
        "status": status,
        "contract": contract,
        "selection_rule": (
            "maximize expected insurer payout minus premium"
            if contract == "nominal_yield"
            else "minimize premium subject to every listed scenario uninsured loss <= capital_loss_limit"
        ),
        "premium_budget": money(budget),
        "snapshot_id": snapshot_id,
        "content_binding": {
            "snapshot_id": snapshot_id,
            "files": files,
            "debtor_mapping": {key: mapping[key] for key in sorted(mapping)},
            "exposures": exposure_rows,
        },
        "offers": [_public_offer(offer) for offer in offers],
        "excluded_offers": excluded,
        "scenarios": [
            {"scenario_id": s["scenario_id"], "probability": str(s["probability"]), "defaults": s["defaults"]}
            for s in scenarios
        ],
        "feasible_combinations": [
            {
                "offer_ids": combo["offer_ids"],
                "premium": money(combo["premium"]),
                "expected_payout": money(combo["expected_payout"]),
                "worst_uninsured_loss": money(combo["worst_uninsured"]),
            }
            for combo in feasible
        ],
        "infeasible_combinations": infeasible,
        "selected": selected,
    }
    if limit is not None:
        certificate["capital_loss_limit"] = money(limit)
    if blocking:
        certificate["blocking_scenarios"] = blocking
    certificate["plan_fingerprint"] = object_fingerprint(
        {
            "schema": PLAN_SCHEMA,
            "snapshot_id": snapshot_id,
            "files": files,
            "debtor_mapping": certificate["content_binding"]["debtor_mapping"],
            "exposures": exposure_rows,
            "offers": certificate["offers"],
            "scenarios": certificate["scenarios"],
            "premium_budget": certificate["premium_budget"],
            "capital_loss_limit": certificate.get("capital_loss_limit"),
            "contract": contract,
            "status": status,
            "selected_offer_ids": selected["offer_ids"] if selected else [],
        }
    )
    return certificate
