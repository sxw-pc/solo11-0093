"""Deterministic partition-license auction with owner royalty settlement."""

from __future__ import annotations

from itertools import combinations

from lakecommit.errors import SchemaError
from lakecommit.license_market import ROW_WEIGHTED, Bid, LicenseIntake, RightsCatalog, parse_intake
from lakecommit.license_royalty import money, row_weighted_payouts, standalone_payouts

CANDIDATE_LIMIT = 5


def _floors(intake: LicenseIntake) -> dict[str, int]:
    """Each region's floor is the best single-region bid for that region."""
    floors = {region: 0 for region in intake.regions}
    for bid in intake.bids:
        if len(bid.regions) == 1:
            floors[bid.regions[0]] = max(floors[bid.regions[0]], bid.value_cents)
    return floors


def _eligibility(intake: LicenseIntake, contract: str) -> tuple[dict[str, int] | None, dict[str, int]]:
    if contract == ROW_WEIGHTED:
        return None, {bid.bid_id: 0 for bid in intake.bids}
    floors = _floors(intake)
    required = {bid.bid_id: sum(floors[region] for region in bid.regions) for bid in intake.bids}
    return floors, required


def _eligible(bid: Bid, contract: str, required: dict[str, int]) -> bool:
    return contract == ROW_WEIGHTED or bid.value_cents >= required[bid.bid_id]


def _allocations(intake: LicenseIntake, contract: str, required: dict[str, int]) -> list[tuple[Bid, ...]]:
    """Enumerate every region-disjoint allocation with one bid per licensee."""
    eligible = [bid for bid in intake.bids if _eligible(bid, contract, required)]
    found = []
    for size in range(len(eligible) + 1):
        for chosen in combinations(eligible, size):
            regions: set[str] = set()
            licensees: set[str] = set()
            for bid in chosen:
                if bid.licensee in licensees or regions & set(bid.regions):
                    break
                licensees.add(bid.licensee)
                regions.update(bid.regions)
            else:
                found.append(chosen)
    return found


def _rank(chosen: tuple[Bid, ...]) -> tuple:
    total = sum(bid.value_cents for bid in chosen)
    licensees = {bid.licensee for bid in chosen}
    return (-total, len(licensees), tuple(sorted(bid.bid_id for bid in chosen)))


def _candidate(chosen: tuple[Bid, ...], is_winner: bool) -> dict:
    total = sum(bid.value_cents for bid in chosen)
    licensees = {bid.licensee for bid in chosen}
    return {
        "bid_ids": sorted(bid.bid_id for bid in chosen),
        "licensees": sorted(licensees),
        "licensee_count": len(licensees),
        "regions": sorted({region for bid in chosen for region in bid.regions}),
        "value_cents": total,
        "value": money(total),
        "winner": is_winner,
    }


def _unsold(intake: LicenseIntake, winners: list[Bid], contract: str,
            required: dict[str, int], floors: dict[str, int] | None,
            allocations: list[tuple[Bid, ...]]) -> list[dict]:
    sold = {region for bid in winners for region in bid.regions}
    winning_licensees = {bid.licensee for bid in winners}
    rows = []
    for region in intake.regions:
        if region in sold:
            continue
        eligible = [bid for bid in intake.bids
                    if region in bid.regions and _eligible(bid, contract, required)]
        sellable = [bid.bid_id for bid in eligible if any(
            bid.bid_id in {chosen_bid.bid_id for chosen_bid in chosen} for chosen in allocations
        )]
        if not eligible:
            reason = "no_contract_eligible_bid"
        elif not sellable:
            reason = "eligible_bids_blocked_by_regions_or_licensee_exclusivity"
        elif any(bid.licensee not in winning_licensees for bid in eligible):
            reason = "eligible_bids_not_selected"
        else:
            reason = "eligible_bids_blocked_by_licensee_conflict"
        item = {
            "region": region,
            "owner": intake.owners[region],
            "reason": reason,
            "eligible_bids": [bid.bid_id for bid in eligible],
            "sellable_bids": sellable,
        }
        if floors is not None:
            item["floor_cents"] = floors[region]
            item["floor"] = money(floors[region])
        rows.append(item)
    return rows


def _conservation(winners: list[Bid], payouts: list[dict]) -> dict:
    per_bid = []
    for bid in winners:
        paid = sum(
            source["amount_cents"]
            for item in payouts for source in item["sources"]
            if source["bid_id"] == bid.bid_id
        )
        per_bid.append({
            "bid_id": bid.bid_id,
            "licensee": bid.licensee,
            "regions": list(bid.regions),
            "sale_cents": bid.value_cents,
            "sale": money(bid.value_cents),
            "payout_cents": paid,
            "payout": money(paid),
            "balanced": paid == bid.value_cents,
        })
    sale_cents = sum(bid.value_cents for bid in winners)
    payout_cents = sum(item["amount_cents"] for item in payouts)
    return {
        "per_bid": per_bid,
        "batch_sale_cents": sale_cents,
        "batch_payout_cents": payout_cents,
        "batch_sale": money(sale_cents),
        "batch_payout": money(payout_cents),
        "balanced": payout_cents == sale_cents and all(item["balanced"] for item in per_bid),
    }


def _contract_result(intake: LicenseIntake, catalog: RightsCatalog, contract: str,
                     floors: dict[str, int] | None, required: dict[str, int]) -> dict:
    allocations = sorted(_allocations(intake, contract, required), key=_rank)
    winner = allocations[0] if allocations else ()
    winners = sorted(winner, key=lambda bid: bid.bid_id)
    if contract == ROW_WEIGHTED:
        payouts = row_weighted_payouts(intake, catalog, winners)
    else:
        payouts = standalone_payouts(intake, floors or {}, winners)
    conservation = _conservation(winners, payouts)
    if not conservation["balanced"]:
        raise SchemaError("ROYALTY_CONSERVATION_FAILED", "owner payouts do not equal sale proceeds")
    eligibility = []
    for bid in intake.bids:
        item = {
            "bid_id": bid.bid_id,
            "licensee": bid.licensee,
            "regions": list(bid.regions),
            "value_cents": bid.value_cents,
            "value": money(bid.value_cents),
            "eligible": _eligible(bid, contract, required),
        }
        if floors is not None:
            item["required_floor_cents"] = required[bid.bid_id]
            item["required_floor"] = money(required[bid.bid_id])
            if not item["eligible"]:
                item["reason"] = "sale_price_below_owner_floors"
        eligibility.append(item)
    return {
        "contract": contract,
        "royalty_rule": "partition_row_share" if contract == ROW_WEIGHTED
        else "owner_outside_option_then_equal_surplus",
        "floors": None if floors is None else {
            region: {"amount_cents": floors[region], "amount": money(floors[region])}
            for region in sorted(floors)
        },
        "bid_eligibility": eligibility,
        "winners": [
            {"bid_id": bid.bid_id, "licensee": bid.licensee, "regions": list(bid.regions),
             "value_cents": bid.value_cents, "value": money(bid.value_cents)}
            for bid in winners
        ],
        "total_value_cents": conservation["batch_sale_cents"],
        "total_value": conservation["batch_sale"],
        "unsold_regions": _unsold(intake, winners, contract, required, floors, allocations),
        "payouts": payouts,
        "conservation": conservation,
        "candidates": [_candidate(chosen, chosen == winner) for chosen in allocations[:CANDIDATE_LIMIT]],
        "candidate_count": len(allocations),
    }


def sell(catalog: RightsCatalog, intake_data: dict) -> dict:
    """Enumerate legal allocations and settle each declared royalty contract.

    If the intake carries a prior certificate, it must belong to exactly this
    snapshot identity; a certificate cannot be replayed against new file rights.
    """
    if isinstance(intake_data, dict) and "certificate" in intake_data:
        verify_certificate(intake_data["certificate"], catalog)
    intake = parse_intake(intake_data, catalog)
    contracts = {}
    for contract in sorted(intake.contracts):
        floors, required = _eligibility(intake, contract)
        contracts[contract] = _contract_result(intake, catalog, contract, floors, required)
    identity = catalog.identity()
    return {
        "snapshot": identity,
        "certificate": {
            "snapshot_id": catalog.snapshot_id,
            "table": catalog.table,
            "manifest_hash": catalog.manifest_hash,
            "rights_fingerprint": identity["fingerprint"],
            "contract_results": {
                name: {
                    "winners": contracts[name]["winners"],
                    "total_value_cents": contracts[name]["total_value_cents"],
                    "payouts": contracts[name]["payouts"],
                    "balanced": contracts[name]["conservation"]["balanced"],
                }
                for name in contracts
            },
        },
        "contracts": contracts,
        "disclaimer": "Synthetic exercise only; no real data rights are granted.",
    }


def verify_certificate(certificate: dict, catalog: RightsCatalog) -> dict:
    """Reject a certificate whose fixed snapshot identity does not match the catalog."""
    if not isinstance(certificate, dict) or not certificate.get("snapshot_id"):
        raise SchemaError("CERTIFICATE_INVALID", "certificate must bind a snapshot identity")
    if certificate.get("snapshot_id") != catalog.snapshot_id:
        raise SchemaError(
            "CERTIFICATE_SNAPSHOT_MISMATCH",
            "certificate belongs to a different snapshot and cannot be reused",
            details={
                "certificate_snapshot_id": certificate.get("snapshot_id"),
                "catalog_snapshot_id": catalog.snapshot_id,
            },
        )
    identity = catalog.identity()
    if (certificate.get("manifest_hash") != identity["manifest_hash"]
            or certificate.get("rights_fingerprint") != identity["fingerprint"]):
        raise SchemaError("CERTIFICATE_IDENTITY_MISMATCH", "certificate snapshot identity no longer matches")
    return {
        "ok": True,
        "snapshot_id": catalog.snapshot_id,
        "manifest_hash": identity["manifest_hash"],
        "rights_fingerprint": identity["fingerprint"],
    }
