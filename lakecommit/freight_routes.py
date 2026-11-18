"""Exact bounded route and fleet search for synthetic co-delivery freight.

A trip always starts and ends at the depot.  Every merchant is served exactly
once across a fleet, and no trip may exceed the vehicle capacity measured in
merchant units.  Distances come from directed fixture edges, so asymmetric
matrices are handled without assuming ``d(x, y) == d(y, x)``.

The search is deliberately exhaustive (at most six merchants, capacity three):
coalition optima must not be guessed by trimming the grand-coalition route.
"""

from __future__ import annotations

from functools import lru_cache

MAX_MERCHANTS = 6
MAX_CAPACITY = 3


class FreightRouteError(ValueError):
    """Stable validation failure for the freight fixture or its network."""


def validate_fixture(data: dict) -> None:
    if not isinstance(data, dict):
        raise FreightRouteError("freight fixture must be a JSON object")
    depot = data.get("depot")
    if not isinstance(depot, str) or not depot:
        raise FreightRouteError("depot must be a non-empty stop id")
    capacity = data.get("vehicle_capacity")
    if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity <= 0:
        raise FreightRouteError("vehicle_capacity must be a positive integer")
    if capacity > MAX_CAPACITY:
        raise FreightRouteError(f"vehicle_capacity exceeds bound {MAX_CAPACITY}")
    merchants = data.get("merchants")
    if not isinstance(merchants, list) or not merchants:
        raise FreightRouteError("merchants must be a non-empty list")
    if len(merchants) > MAX_MERCHANTS:
        raise FreightRouteError(f"merchant count exceeds bound {MAX_MERCHANTS}")
    merchant_ids: list[str] = []
    stops: list[str] = []
    order_ids: list[str] = []
    for index, merchant in enumerate(merchants):
        if not isinstance(merchant, dict):
            raise FreightRouteError(f"merchants[{index}] must be an object")
        mid = merchant.get("merchant_id")
        stop = merchant.get("stop")
        units = merchant.get("units")
        order_id = merchant.get("order_id")
        if not isinstance(mid, str) or not mid:
            raise FreightRouteError(f"merchants[{index}].merchant_id must be a non-empty string")
        if not isinstance(stop, str) or not stop:
            raise FreightRouteError(f"merchants[{index}].stop must be a non-empty string")
        if not isinstance(units, int) or isinstance(units, bool) or units <= 0:
            raise FreightRouteError(f"merchants[{index}].units must be a positive integer")
        if not isinstance(order_id, str) or not order_id:
            raise FreightRouteError(f"merchants[{index}].order_id must be a non-empty string")
        merchant_ids.append(mid)
        stops.append(stop)
        order_ids.append(order_id)
    if len(set(merchant_ids)) != len(merchant_ids):
        raise FreightRouteError("duplicate merchant_id in fixture")
    if len(set(stops)) != len(stops):
        raise FreightRouteError("duplicate stop in fixture")
    if len(set(order_ids)) != len(order_ids):
        raise FreightRouteError("duplicate order_id in fixture")
    if depot in set(stops):
        raise FreightRouteError("a merchant stop may not reuse the depot id")
    _validate_network(data, depot, stops, capacity, merchants)


def _validate_network(data: dict, depot: str, stops: list[str], capacity: int, merchants: list) -> None:
    distance = data.get("distance")
    if not isinstance(distance, dict):
        raise FreightRouteError("distance must be an object of directed edges")
    nodes = [depot, *stops]
    for source in nodes:
        outgoing = distance.get(source)
        if not isinstance(outgoing, dict):
            raise FreightRouteError(f"missing directed edges from stop: {source}")
        for target in nodes:
            if source == target:
                continue
            edge = outgoing.get(target)
            if not isinstance(edge, int) or isinstance(edge, bool):
                raise FreightRouteError(f"missing directed edge: {source} -> {target}")
            if edge < 0:
                raise FreightRouteError(f"negative distance on edge: {source} -> {target}")
    if sum(int(m["units"]) for m in merchants) <= 0:
        raise FreightRouteError("total carrier units must be positive")
    for merchant in merchants:
        if int(merchant["units"]) > capacity:
            raise FreightRouteError(
                f"merchant {merchant['merchant_id']} needs {merchant['units']} units, over capacity {capacity}"
            )


class RouteNetwork:
    """Directed distance graph with memoized exhaustive route costs."""

    def __init__(self, data: dict) -> None:
        validate_fixture(data)
        self.depot: str = data["depot"]
        self.capacity: int = int(data["vehicle_capacity"])
        self.merchants = [dict(m) for m in data["merchants"]]
        self.merchant_ids = tuple(m["merchant_id"] for m in self.merchants)
        self.stop_by_merchant = {m["merchant_id"]: m["stop"] for m in self.merchants}
        self.units_by_merchant = {m["merchant_id"]: int(m["units"]) for m in self.merchants}
        self._distance = data["distance"]

    def edge(self, source: str, target: str) -> int:
        value = self._distance[source][target]
        return int(value)

    def trip_units(self, members: tuple[str, ...]) -> int:
        return sum(self.units_by_merchant[mid] for mid in members)

    def trip_route(self, members: tuple[str, ...]) -> dict:
        """Cheapest depot->permutation->depot route serving exactly these merchants."""
        if not members:
            raise FreightRouteError("a trip must carry at least one merchant")
        if self.trip_units(members) > self.capacity:
            raise FreightRouteError("trip would exceed vehicle capacity")
        best: dict | None = None
        for permutation in _permutations(members):
            order = [self.depot, *(self.stop_by_merchant[mid] for mid in permutation), self.depot]
            cost = sum(self.edge(order[i], order[i + 1]) for i in range(len(order) - 1))
            if best is None or cost < best["cost"]:
                best = {"members": tuple(permutation), "stops": tuple(order), "cost": int(cost)}
        assert best is not None
        return best

    @lru_cache(maxsize=None)
    def cheapest_trip(self, members: tuple[str, ...]) -> dict:
        return self.trip_route(members)

    def coalition_fleet(self, coalition: tuple[str, ...]) -> dict:
        """Cheapest legal fleet (set of capacity-feasible trips) for a coalition.

        Every coalition gets its own exhaustive search; deleting a merchant from
        the grand-coalition solution is never used as a sub-coalition optimum.
        """
        members = tuple(sorted(coalition))
        unknown = [mid for mid in members if mid not in self.stop_by_merchant]
        if unknown:
            raise FreightRouteError(f"unknown merchant in coalition: {unknown[0]}")
        if not members:
            return {"coalition": (), "trips": (), "cost": 0}
        best: dict | None = None
        for groups in _set_partitions(members):
            if any(self.trip_units(group) > self.capacity for group in groups):
                continue
            trips = tuple(self.cheapest_trip(group) for group in groups)
            total = sum(trip["cost"] for trip in trips)
            candidate = {"coalition": members, "trips": trips, "cost": int(total)}
            if best is None or total < best["cost"]:
                best = candidate
        if best is None:
            raise FreightRouteError("coalition cannot be served within vehicle capacity")
        return best

    def all_coalition_costs(self) -> dict[tuple[str, ...], dict]:
        result: dict[tuple[str, ...], dict] = {(): {"coalition": (), "trips": (), "cost": 0}}
        ids = self.merchant_ids
        for mask in range(1, 1 << len(ids)):
            coalition = tuple(ids[i] for i in range(len(ids)) if mask & (1 << i))
            result[coalition] = self.coalition_fleet(coalition)
        return result


def _permutations(items: tuple[str, ...]) -> list[tuple[str, ...]]:
    if len(items) <= 1:
        return [items]
    out: list[tuple[str, ...]] = []
    for index, item in enumerate(items):
        rest = items[:index] + items[index + 1:]
        out.extend((item, *tail) for tail in _permutations(rest))
    return out


def _set_partitions(items: tuple[str, ...]) -> list[tuple[tuple[str, ...], ...]]:
    """All set partitions, each block internally sorted for stable evidence."""
    if not items:
        return [()]
    head = items[0]
    out: list[tuple[tuple[str, ...], ...]] = []
    for partition in _set_partitions(items[1:]):
        blocks = list(partition)
        out.append(((head,), *blocks))
        for index in range(len(blocks)):
            merged = sorted((*blocks[index], head))
            new_blocks = sorted((*blocks[:index], tuple(merged), *blocks[index + 1:]))
            out.append(tuple(new_blocks))
    return out
