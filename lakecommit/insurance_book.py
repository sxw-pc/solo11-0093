"""Offer book loading and validation for the bounded insurance planner.

An offer book is synthetic JSON: an order->debtor mapping, at most
``MAX_OFFERS`` quotes, at most ``MAX_SCENARIOS`` joint default scenarios, a
premium budget and the independently pre-authorized contracts.  Anything the
planner cannot evaluate honestly (duplicate keys, missing debtors, negative or
mis-summing probabilities) is rejected as indeterminate instead of guessed.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

from lakecommit.errors import LakeCommitError

MAX_OFFERS = 8
MAX_SCENARIOS = 6
CENT = Decimal("0.01")


class PlanIndeterminateError(LakeCommitError):
    """The inputs cannot be evaluated honestly, so no purchase is guessed."""

    exit_code = 3
    default_code = "PLAN_INDETERMINATE"


def _reject(code: str, message: str, **details: object) -> None:
    raise PlanIndeterminateError(code, message, details={k: v for k, v in details.items() if v} or None)


def money(value: Decimal) -> str:
    return str(value.quantize(CENT))


def decimal_field(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        _reject("PLAN_FIELD_INVALID", f"{field} must be a decimal string", field=field)
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        _reject("PLAN_FIELD_INVALID", f"{field} must be a decimal string", field=field)
    if not result.is_finite():
        _reject("PLAN_FIELD_INVALID", f"{field} must be finite", field=field)
    return result


def _unique_object(pairs: list) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            _reject("PLAN_DUPLICATE_KEY", "offer book JSON contains a duplicate object key", key=key)
        result[key] = value
    return result


def load_offer_book(path: str | Path) -> dict:
    """Parse an offer book, rejecting duplicate keys such as an invoice mapped twice."""
    try:
        book = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except PlanIndeterminateError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanIndeterminateError(
            "PLAN_OFFER_BOOK_INVALID",
            "offer book cannot be read as a JSON object",
            details={"reason": exc.__class__.__name__},
        ) from exc
    if not isinstance(book, dict):
        _reject("PLAN_OFFER_BOOK_INVALID", "offer book must be a JSON object")
    return book


def debtor_map(book: dict) -> dict[str, str]:
    mapping = book.get("debtors")
    if not isinstance(mapping, dict) or not mapping:
        _reject("PLAN_DEBTORS_MISSING", "offer book must map order ids to debtor ids")
    for order_id, debtor_id in mapping.items():
        if not isinstance(order_id, str) or not isinstance(debtor_id, str) or not order_id or not debtor_id:
            _reject("PLAN_DEBTORS_MISSING", "debtor mapping entries must be non-empty strings")
    return {str(order_id): str(debtor_id) for order_id, debtor_id in mapping.items()}


def validate_offers(book: dict, exposure_ids: set) -> tuple[list, list]:
    """Normalize offers; quotes naming debtors outside the portfolio are not purchasable."""
    raw = book.get("offers", [])
    if not isinstance(raw, list) or len(raw) > MAX_OFFERS:
        _reject("PLAN_OFFER_BOUNDS", f"offers must be a list of at most {MAX_OFFERS} entries")
    seen: set = set()
    offers, excluded = [], []
    for entry in raw:
        if not isinstance(entry, dict):
            _reject("PLAN_OFFER_INVALID", "each offer must be an object")
        offer_id = entry.get("offer_id")
        if not isinstance(offer_id, str) or not offer_id:
            _reject("PLAN_OFFER_INVALID", "each offer needs a non-empty offer_id")
        if offer_id in seen:
            _reject("PLAN_DUPLICATE_OFFER_ID", "duplicate offer_id in offer book", offer_id=offer_id)
        seen.add(offer_id)
        debtors = entry.get("debtors")
        if not isinstance(debtors, list) or not debtors or any(not isinstance(d, str) or not d for d in debtors):
            _reject("PLAN_OFFER_DEBTORS_MISSING", "offer does not name the debtors it covers", offer_id=offer_id)
        if len(set(debtors)) != len(debtors):
            _reject("PLAN_OFFER_INVALID", "offer lists a debtor twice", offer_id=offer_id)
        premium = decimal_field(entry.get("premium"), "premium")
        if premium < 0:
            _reject("PLAN_PREMIUM_NEGATIVE", "premium must not be negative", offer_id=offer_id)
        fraction = decimal_field(entry.get("coverage_fraction", "1"), "coverage_fraction")
        if not Decimal(0) <= fraction <= Decimal(1):
            _reject("PLAN_COVERAGE_INVALID", "coverage_fraction must be within [0, 1]", offer_id=offer_id)
        deductible = decimal_field(entry.get("deductible", "0"), "deductible")
        if deductible < 0:
            _reject("PLAN_DEDUCTIBLE_NEGATIVE", "deductible must not be negative", offer_id=offer_id)
        raw_limit = entry.get("aggregate_limit")
        aggregate = decimal_field(raw_limit, "aggregate_limit") if raw_limit is not None else None
        if aggregate is not None and aggregate <= 0:
            _reject("PLAN_AGGREGATE_LIMIT_INVALID", "aggregate_limit must be positive", offer_id=offer_id)
        unknown = sorted(set(debtors) - exposure_ids)
        if unknown:
            excluded.append(
                {"offer_id": offer_id, "reason": "debtor_not_in_portfolio", "unknown_debtors": unknown}
            )
            continue
        offers.append(
            {
                "offer_id": offer_id,
                "debtors": sorted(set(debtors)),
                "premium": premium,
                "coverage_fraction": fraction,
                "deductible": deductible,
                "aggregate_limit": aggregate,
            }
        )
    return sorted(offers, key=lambda offer: offer["offer_id"]), excluded


def validate_scenarios(book: dict, exposure_ids: set) -> list:
    """Normalize joint default scenarios; probabilities must be non-negative and sum to 1."""
    raw = book.get("scenarios")
    if not isinstance(raw, list) or not raw or len(raw) > MAX_SCENARIOS:
        _reject("PLAN_SCENARIO_BOUNDS", f"scenarios must be a list of 1..{MAX_SCENARIOS} entries")
    seen: set = set()
    total = Decimal(0)
    scenarios = []
    for entry in raw:
        if not isinstance(entry, dict):
            _reject("PLAN_SCENARIO_INVALID", "each scenario must be an object")
        scenario_id = entry.get("scenario_id")
        if not isinstance(scenario_id, str) or not scenario_id:
            _reject("PLAN_SCENARIO_INVALID", "each scenario needs a non-empty scenario_id")
        if scenario_id in seen:
            _reject("PLAN_DUPLICATE_SCENARIO_ID", "duplicate scenario_id", scenario_id=scenario_id)
        seen.add(scenario_id)
        probability = decimal_field(entry.get("probability"), "probability")
        if probability < 0:
            _reject(
                "PLAN_PROBABILITY_NEGATIVE", "scenario probability must not be negative", scenario_id=scenario_id
            )
        defaults = entry.get("defaults")
        if not isinstance(defaults, list) or any(not isinstance(d, str) or not d for d in defaults):
            _reject("PLAN_SCENARIO_INVALID", "scenario defaults must be debtor ids", scenario_id=scenario_id)
        unknown = sorted(set(defaults) - exposure_ids)
        if unknown:
            _reject(
                "PLAN_SCENARIO_DEBTOR_UNKNOWN",
                "scenario defaults a debtor outside the verified portfolio",
                scenario_id=scenario_id,
                unknown_debtors=unknown,
            )
        total += probability
        scenarios.append({"scenario_id": scenario_id, "probability": probability, "defaults": sorted(set(defaults))})
    if total != 1:
        _reject("PLAN_PROBABILITY_SUM", "scenario probabilities must sum to exactly 1", total=str(total))
    return scenarios
