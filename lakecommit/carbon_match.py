"""Exact certificate->load matching for synthetic carbon retirement contracts.

The solver works on integer-scaled units and never on arrival order: it finds
a maximum-coverage allocation on the contract's legal edges, then breaks ties
lexicographically by (certificate_id, load_id) so results are reproducible.
"""
from __future__ import annotations

from collections import deque
from decimal import Decimal

MATCHING_REGION_HOUR = "region_hour"
MATCHING_ANNUAL_TRANSFER = "annual_transfer"
MATCHINGS = (MATCHING_REGION_HOUR, MATCHING_ANNUAL_TRANSFER)
ALGORITHM = "exact bipartite b-matching (max-flow with lexicographic certificate_id/load_id tie-break)"


def fmt_units(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def edge_allowed(matching: str, cert: dict, load: dict) -> bool:
    region_hour = cert["region"] == load["region"] and load["hour"] in cert["eligible_hours"]
    if matching == MATCHING_REGION_HOUR:
        return region_hour
    # annual_transfer contracts: authorized certificates may be spent once on
    # any load in the batch; unauthorized ones keep their region/hour limits.
    return cert["annual_transfer"] or region_hour


def max_flow(supplies: list[int], demands: list[int], edges: list[tuple[int, int]]) -> int:
    """Exact integer max-flow on the source->certificates->loads->sink network."""
    offset = len(supplies)
    sink = offset + len(demands) + 1
    graph = [[] for _ in range(sink + 1)]

    def add_arc(u: int, v: int, cap: int) -> None:
        graph[u].append([v, cap, len(graph[v])])
        graph[v].append([u, 0, len(graph[u]) - 1])

    for i, supply in enumerate(supplies):
        if supply:
            add_arc(0, 1 + i, supply)
    for i, j in edges:
        add_arc(1 + i, 1 + offset + j, min(supplies[i], demands[j]))
    for j, demand in enumerate(demands):
        if demand:
            add_arc(1 + offset + j, sink, demand)
    total = 0
    while True:
        prev = [None] * (sink + 1)
        prev[0] = (0, -1)
        queue = deque([0])
        while queue and prev[sink] is None:
            u = queue.popleft()
            for arc_index, arc in enumerate(graph[u]):
                if arc[1] > 0 and prev[arc[0]] is None:
                    prev[arc[0]] = (u, arc_index)
                    queue.append(arc[0])
        if prev[sink] is None:
            return total
        flow = None
        node = sink
        while node != 0:
            u, arc_index = prev[node]
            arc = graph[u][arc_index]
            flow = arc[1] if flow is None else min(flow, arc[1])
            node = u
        node = sink
        while node != 0:
            u, arc_index = prev[node]
            arc = graph[u][arc_index]
            arc[1] -= flow
            graph[node][arc[2]][1] += flow
            node = u
        total += flow


def optimal_allocation(
    supplies: list[int], demands: list[int], edges: list[tuple[int, int]]
) -> dict[tuple[int, int], int]:
    """Maximize covered units, then lexicographically maximize edge flow in the
    given (certificate_id, load_id)-sorted edge order.

    Each edge is fixed, in order, to the largest value that still lets the
    remaining edges complete a maximum flow.  The values an edge carries
    across maximum flows form an interval, so "some maximum flow gives this
    edge at least ``mid``" is monotone and binary search finds the maximum
    exactly; the edge stays in the residual network during the search and is
    only removed once its value is fixed.
    """
    target = max_flow(supplies, demands, edges)
    remaining_supply = list(supplies)
    remaining_demand = list(demands)
    remaining_edges = list(edges)
    allocation = {}
    for edge in edges:
        i, j = edge
        lo, hi = 0, min(remaining_supply[i], remaining_demand[j])
        while lo < hi:
            mid = (lo + hi + 1) // 2
            trial_supply = list(remaining_supply)
            trial_demand = list(remaining_demand)
            trial_supply[i] -= mid
            trial_demand[j] -= mid
            if max_flow(trial_supply, trial_demand, remaining_edges) >= target - mid:
                lo = mid
            else:
                hi = mid - 1
        if lo:
            allocation[edge] = lo
        remaining_supply[i] -= lo
        remaining_demand[j] -= lo
        target -= lo
        remaining_edges.remove(edge)
    return allocation


def solve_contract(loads: list[dict], certs: list[dict], matching: str, max_uncovered: Decimal) -> dict:
    """Optimal retirement evidence for one contract over its legal edges."""
    quantities = [load["units"] for load in loads] + [cert["units"] for cert in certs] + [max_uncovered]
    scale = 1
    for quantity in quantities:
        exponent = quantity.as_tuple().exponent
        if exponent < 0:
            scale = max(scale, 10 ** (-exponent))
    supplies = [int(cert["units"] * scale) for cert in certs]
    demands = [int(load["units"] * scale) for load in loads]
    edges = [
        (ci, li)
        for ci, cert in enumerate(certs)
        for li, load in enumerate(loads)
        if edge_allowed(matching, cert, load)
    ]
    allocation = optimal_allocation(supplies, demands, edges)
    used = [0] * len(certs)
    covered = [0] * len(loads)
    for (ci, li), flow in allocation.items():
        used[ci] += flow
        covered[li] += flow
    total_uncovered = sum(demands) - sum(covered)

    def units(value: int) -> str:
        return fmt_units(Decimal(value) / Decimal(scale))

    return {
        "matching": matching,
        "algorithm": ALGORITHM,
        "legal_edges": len(edges),
        "max_uncovered": fmt_units(max_uncovered),
        "total_load": units(sum(demands)),
        "total_covered": units(sum(covered)),
        "total_uncovered": units(total_uncovered),
        "issued": total_uncovered <= int(max_uncovered * scale),
        "allocations": [
            {
                "certificate_id": certs[ci]["certificate_id"],
                "load_id": loads[li]["load_id"],
                "units": units(flow),
            }
            for (ci, li), flow in sorted(allocation.items())
        ],
        "certificate_remaining": {
            cert["certificate_id"]: units(supply - spent)
            for cert, supply, spent in zip(certs, supplies, used)
        },
        "uncovered_loads": {
            load["load_id"]: units(demand - got) for load, demand, got in zip(loads, demands, covered)
        },
    }
