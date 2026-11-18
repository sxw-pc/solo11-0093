"""Deterministic owner royalty settlement for fictional partition licenses."""

from __future__ import annotations

from lakecommit.license_market import Bid, LicenseIntake, RightsCatalog


def money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def row_shares(total: int, regions: list[str], rows: dict[str, int]) -> dict[str, int]:
    """Floor each row-weighted share, then hand residual cents out in region order.

    A region set with no committed rows falls back to equal weights so the sale
    still settles rather than dividing by zero.
    """
    denominator = sum(rows[region] for region in regions)
    weights = {region: rows[region] for region in regions}
    if denominator == 0:
        weights = {region: 1 for region in regions}
        denominator = len(regions)
    shares = {region: total * weights[region] // denominator for region in regions}
    residual = total - sum(shares.values())
    ranking = sorted(
        regions,
        key=lambda region: (-(total * weights[region] % denominator), region),
    )
    for region in ranking[:residual]:
        shares[region] += 1
    return {region: shares[region] for region in regions}


def payout_rows(owner_cents: dict[str, int], sources: list[dict]) -> list[dict]:
    return [
        {
            "owner": owner,
            "amount_cents": owner_cents[owner],
            "amount": money(owner_cents[owner]),
            "sources": [item for item in sources if item["owner"] == owner],
        }
        for owner in sorted(owner_cents)
    ]


def row_weighted_payouts(intake: LicenseIntake, catalog: RightsCatalog,
                         winners: list[Bid]) -> list[dict]:
    owner_cents: dict[str, int] = {}
    sources: list[dict] = []
    for bid in winners:
        regions = list(bid.regions)
        shares = row_shares(bid.value_cents, regions, catalog.row_counts)
        for region in regions:
            owner = intake.owners[region]
            owner_cents[owner] = owner_cents.get(owner, 0) + shares[region]
            sources.append({
                "bid_id": bid.bid_id,
                "region": region,
                "owner": owner,
                "row_weight": catalog.row_counts[region],
                "amount_cents": shares[region],
            })
    return payout_rows(owner_cents, sources)


def standalone_payouts(intake: LicenseIntake, floors: dict[str, int],
                       winners: list[Bid]) -> list[dict]:
    """Pay each owner its own floor, then split surplus equally in stable owner order."""
    owner_cents: dict[str, int] = {}
    sources: list[dict] = []
    for bid in winners:
        owners = sorted({intake.owners[region] for region in bid.regions})
        required = sum(floors[region] for region in bid.regions)
        share, remainder = divmod(bid.value_cents - required, len(owners))
        for position, owner in enumerate(owners):
            floor_cents = sum(
                floors[region] for region in bid.regions if intake.owners[region] == owner
            )
            extra = share + (1 if position < remainder else 0)
            owner_cents[owner] = owner_cents.get(owner, 0) + floor_cents + extra
            sources.append({
                "bid_id": bid.bid_id,
                "regions": [region for region in bid.regions if intake.owners[region] == owner],
                "owner": owner,
                "floor_cents": floor_cents,
                "surplus_cents": extra,
                "amount_cents": floor_cents + extra,
            })
    return payout_rows(owner_cents, sources)
