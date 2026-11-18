"""Two independently authorized freight billing contracts.

Both contracts charge the full grand-coalition fleet cost and may never exceed
each merchant's verified solo upper bound.  They differ in their billing base:

* ``value_share`` splits the total in cents by verified order amounts.  Cents
  are indivisible, so the remainder is allocated by the largest-remainder rule
  with stable merchant-id tie breaking.
* ``marginal_share`` charges the Shapley value: each merchant's average
  marginal fleet-cost increase across every joining permutation.  Every
  sub-coalition optimum is re-solved by the exact route search rather than
  derived once from the grand coalition.

Fractional Shapley amounts are handled as exact rationals and only converted
to cents at the end, again with conservation-preserving largest remainder.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

CENTS_PER_COST_UNIT = 100


class ContractRejectedError(ValueError):
    """A contract cannot simultaneously keep its rules and the solo bounds."""


def largest_remainder(weights: dict[str, int], total_cents: int) -> dict[str, int]:
    """Split exactly ``total_cents`` by integer weights, conserving the total."""
    if total_cents < 0:
        raise ContractRejectedError("billed total must be non-negative")
    merchants = sorted(weights)
    weight_sum = sum(weights[m] for m in merchants)
    if weight_sum <= 0:
        raise ContractRejectedError("billing weights must sum to a positive value")
    quotas = {m: Fraction(weights[m] * total_cents, weight_sum) for m in merchants}
    floors = {m: int(quotas[m]) for m in merchants}
    remainder = total_cents - sum(floors.values())
    order = sorted(merchants, key=lambda m: (-(quotas[m] - floors[m]), m))
    for merchant in order[:remainder]:
        floors[merchant] += 1
    assert sum(floors.values()) == total_cents
    return floors


def amount_cents(amount: str) -> int:
    """Convert a query_rows normalized decimal amount to an integer cent count."""
    value = Decimal(amount)
    cents = (value * 100).quantize(Decimal("1"))
    if cents != (value * 100):
        raise ContractRejectedError(f"order amount is not cent-exact: {amount}")
    return int(cents)


def value_share_bills(
    merchants: tuple[str, ...], order_amounts: dict[str, str], total_cents: int
) -> dict:
    weights = {mid: amount_cents(order_amounts[mid]) for mid in merchants}
    bills = largest_remainder(weights, total_cents)
    return {
        "rule": "verified_order_amount",
        "weights_cents": {mid: weights[mid] for mid in merchants},
        "bills_cents": {mid: bills[mid] for mid in merchants},
    }


def shapley_charges(
    merchants: tuple[str, ...], coalition_costs: dict[tuple[str, ...], int]
) -> dict[str, Fraction]:
    """Average marginal contribution over all n! joining permutations."""
    n = len(merchants)
    totals = {mid: Fraction(0) for mid in merchants}
    normalized = {tuple(sorted(coalition)): cost for coalition, cost in coalition_costs.items()}
    for permutation in _permutations(merchants):
        members: tuple[str, ...] = ()
        previous = 0
        for mid in permutation:
            members = tuple(sorted((*members, mid)))
            current = int(normalized[members])
            totals[mid] += Fraction(current - previous)
            previous = current
    return {mid: totals[mid] / _factorial(n) for mid in merchants}


def marginal_share_bills(
    merchants: tuple[str, ...], coalition_costs: dict[tuple[str, ...], int], total_cents: int
) -> dict:
    shapley = shapley_charges(merchants, coalition_costs)
    exact_total = sum(shapley.values())
    if exact_total != Fraction(total_cents, CENTS_PER_COST_UNIT):
        raise ContractRejectedError("Shapley values do not conserve the fleet total")
    quotas = {mid: shapley[mid] * CENTS_PER_COST_UNIT for mid in merchants}
    floors = {mid: int(quotas[mid]) for mid in merchants}
    remainder = total_cents - sum(floors.values())
    order = sorted(merchants, key=lambda m: (-(quotas[m] - floors[m]), m))
    bills = dict(floors)
    for merchant in order[:remainder]:
        bills[merchant] += 1
    if sum(bills.values()) != total_cents:
        raise ContractRejectedError("marginal bills do not conserve the fleet total")
    return {
        "rule": "coalition_marginal",
        "shapley_exact": {mid: str(shapley[mid]) for mid in merchants},
        "bills_cents": {mid: bills[mid] for mid in merchants},
    }


def enforce_contract(
    name: str,
    merchants: tuple[str, ...],
    bills_cents: dict[str, int],
    solo_upper_cents: dict[str, int],
    total_cents: int,
) -> dict:
    """Refuse to issue rather than truncate when a rule or bound is violated."""
    if any(bills_cents[mid] < 0 for mid in merchants):
        raise ContractRejectedError(f"contract {name} produced a negative invoice")
    if sum(bills_cents.values()) != total_cents:
        raise ContractRejectedError(f"contract {name} does not conserve the billed total")
    violations = [
        {"merchant_id": mid, "billed_cents": bills_cents[mid], "solo_upper_cents": solo_upper_cents[mid]}
        for mid in merchants
        if bills_cents[mid] > solo_upper_cents[mid]
    ]
    if violations:
        raise ContractRejectedError(f"contract {name} exceeds a verified solo upper bound")
    return {
        "contract": name,
        "total_cents": total_cents,
        "bills_cents": {mid: bills_cents[mid] for mid in merchants},
        "solo_upper_cents": {mid: solo_upper_cents[mid] for mid in merchants},
        "violations": violations,
    }


def _permutations(items: tuple[str, ...]) -> list[tuple[str, ...]]:
    if len(items) <= 1:
        return [items]
    out: list[tuple[str, ...]] = []
    for index, item in enumerate(items):
        rest = items[:index] + items[index + 1:]
        out.extend((item, *tail) for tail in _permutations(rest))
    return out


def _factorial(n: int) -> int:
    result = 1
    for value in range(2, n + 1):
        result *= value
    return result
