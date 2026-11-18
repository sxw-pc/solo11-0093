import copy
import json

import pytest

from lakecommit.freight_contracts import (
    ContractRejectedError,
    largest_remainder,
    marginal_share_bills,
)
from lakecommit.freight_routes import MAX_CAPACITY, MAX_MERCHANTS, RouteNetwork
from test_query_rows import FIXTURES, make_store

from freight_settlement_helpers import FIXTURE, MERCHANTS, settle_fixture


def test_grand_coalition_drops_solo_total_54_to_shared_37(tmp_path):
    settlement, _, _ = settle_fixture(tmp_path)
    assert settlement["solo_total_cost"] == 54
    assert settlement["grand_fleet"]["cost"] == 37
    trips = settlement["grand_fleet"]["trips"]
    assert {tuple(trip["members"]) for trip in trips} == {("m-A", "m-B"), ("m-C",)}
    by_members = {tuple(trip["members"]): trip for trip in trips}
    assert by_members[("m-A", "m-B")]["cost"] == 21
    assert by_members[("m-A", "m-B")]["units"] == 2
    assert by_members[("m-C",)]["cost"] == 16
    assert all(trip["units"] <= trip["capacity"] for trip in trips)


def test_every_sub_coalition_gets_its_own_optimum_with_evidence(tmp_path):
    settlement, _, _ = settle_fixture(tmp_path)
    costs = {tuple(item["coalition"]): item["cost"] for item in settlement["coalition_optima"]}
    expected = {
        (): 0, ("m-A",): 20, ("m-B",): 18, ("m-C",): 16,
        ("m-A", "m-B"): 21, ("m-A", "m-C"): 23, ("m-B", "m-C"): 23,
        ("m-A", "m-B", "m-C"): 37,
    }
    assert costs == expected
    for item in settlement["coalition_optima"]:
        served = tuple(member for trip in item["trips"] for member in trip["members"])
        assert tuple(sorted(served)) == tuple(item["coalition"])
        assert sum(trip["cost"] for trip in item["trips"]) == item["cost"]
        assert all(trip["stops"][0] == "D" and trip["stops"][-1] == "D" for trip in item["trips"])


def test_value_share_invoices_match_verified_amount_weights(tmp_path):
    settlement, _, _ = settle_fixture(tmp_path)
    contract = settlement["contracts"]["value_share"]
    assert contract["rule"] == "verified_order_amount"
    assert contract["weights_cents"] == {"m-A": 900, "m-B": 1100, "m-C": 1000}
    assert contract["bills_cents"] == {"m-A": 1110, "m-B": 1357, "m-C": 1233}
    assert {mid: contract["invoices"][mid]["amount"] for mid in MERCHANTS} == {
        "m-A": "11.10", "m-B": "13.57", "m-C": "12.33",
    }
    assert sum(contract["bills_cents"].values()) == 3700


def test_marginal_share_recomputes_all_permutations(tmp_path):
    settlement, _, _ = settle_fixture(tmp_path)
    contract = settlement["contracts"]["marginal_share"]
    assert contract["rule"] == "coalition_marginal"
    assert contract["shapley_exact"] == {"m-A": "13", "m-B": "12", "m-C": "12"}
    assert contract["bills_cents"] == {"m-A": 1300, "m-B": 1200, "m-C": 1200}
    assert {mid: contract["invoices"][mid]["amount"] for mid in MERCHANTS} == {
        "m-A": "13.00", "m-B": "12.00", "m-C": "12.00",
    }
    assert sum(contract["bills_cents"].values()) == 3700


def test_contracts_are_distinct_but_both_complete_and_within_solo_bounds(tmp_path):
    settlement, _, _ = settle_fixture(tmp_path)
    value_bills = settlement["contracts"]["value_share"]["bills_cents"]
    marginal_bills = settlement["contracts"]["marginal_share"]["bills_cents"]
    assert value_bills != marginal_bills
    for name, bills in (("value_share", value_bills), ("marginal_share", marginal_bills)):
        contract = settlement["contracts"][name]
        assert contract["violations"] == []
        assert sum(bills.values()) == 3700
        for mid in MERCHANTS:
            assert 0 <= bills[mid] <= contract["solo_upper_cents"][mid]


def test_sub_coalitions_re_route_when_edges_change(tmp_path):
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    network = RouteNetwork(data)
    base = network.all_coalition_costs()
    assert base[("m-A", "m-B")]["cost"] == 21
    changed = copy.deepcopy(data)
    changed["distance"]["A"]["B"] = 7
    changed["distance"]["B"]["A"] = 8
    changed_network = RouteNetwork(changed)
    changed_costs = changed_network.all_coalition_costs()
    # both AB permutations now cost 26 (10+7+9 and 9+8+10), above the old 21
    assert changed_costs[("m-A", "m-B")]["cost"] == 26
    assert changed_costs[MERCHANTS]["cost"] != base[MERCHANTS]["cost"]
    new_total = changed_costs[MERCHANTS]["cost"] * 100
    marginal = marginal_share_bills(
        MERCHANTS, {c: s["cost"] for c, s in changed_costs.items()}, new_total
    )
    assert sum(marginal["bills_cents"].values()) == new_total
    assert marginal["bills_cents"] != {"m-A": 1300, "m-B": 1200, "m-C": 1200}


def test_directed_asymmetry_is_respected():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data["distance"]["A"]["B"] = 4
    data["distance"]["B"]["A"] = 9
    network = RouteNetwork(data)
    ab = network.coalition_fleet(("m-A", "m-B"))
    assert ab["trips"][0]["stops"] == ("D", "A", "B", "D")
    assert ab["cost"] == 10 + 4 + 9
    assert network.edge("A", "B") != network.edge("B", "A")


def test_cent_conservation_under_largest_remainder():
    bills = largest_remainder({"m-A": 1, "m-B": 1, "m-C": 1}, 100)
    assert sum(bills.values()) == 100
    assert set(bills.values()) <= {33, 34}
    again = largest_remainder({"m-C": 1, "m-A": 1, "m-B": 1}, 100)
    assert again == bills
    with pytest.raises(ContractRejectedError):
        largest_remainder({"m-A": 0}, 10)


def test_input_reordering_does_not_change_the_settlement(tmp_path):
    first, _, _ = settle_fixture(tmp_path)
    reordered = json.loads(FIXTURE.read_text(encoding="utf-8"))
    reordered["merchants"] = list(reversed(reordered["merchants"]))
    second, _, _ = settle_fixture(tmp_path / "again", reordered)

    def canonical(value):
        if isinstance(value, dict):
            return {key: canonical(item) for key, item in value.items()}
        if isinstance(value, list):
            if value and all(isinstance(item, str) for item in value) and value[0] == "D" and value[-1] == "D":
                return value
            return sorted((canonical(item) for item in value), key=json.dumps)
        return value

    assert canonical(second) == canonical(first)
    assert second["grand_fleet"]["cost"] == first["grand_fleet"]["cost"] == 37


def test_naive_equal_splits_are_neither_authorized_contract(tmp_path):
    settlement, _, _ = settle_fixture(tmp_path)
    value_bills = settlement["contracts"]["value_share"]["bills_cents"]
    marginal_bills = settlement["contracts"]["marginal_share"]["bills_cents"]
    # splitting the solo total 54 three ways would bill C 18 over its solo 16
    naive_54 = {mid: 1800 for mid in MERCHANTS}
    assert naive_54["m-C"] > settlement["contracts"]["value_share"]["solo_upper_cents"]["m-C"]
    # even an equal split of the real total 37 matches neither billing base
    equal_37 = {"m-A": 1233, "m-B": 1233, "m-C": 1234}
    assert equal_37 != value_bills
    assert equal_37 != marginal_bills
    assert value_bills == {"m-A": 1110, "m-B": 1357, "m-C": 1233}
    assert marginal_bills == {"m-A": 1300, "m-B": 1200, "m-C": 1200}


def test_capacity_counts_units_not_merchants():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    # m-A alone fills both slots; it cannot share a trip even though two
    # merchants would nominally fit, so AB must split into two solo trips.
    data["merchants"][0]["units"] = 2
    network = RouteNetwork(data)
    ab = network.coalition_fleet(("m-A", "m-B"))
    assert len(ab["trips"]) == 2
    assert ab["cost"] == 20 + 18
    grand = network.coalition_fleet(MERCHANTS)
    for trip in grand["trips"]:
        assert network.trip_units(trip["members"]) <= network.capacity


def test_bounded_search_handles_six_merchants_and_capacity_three():
    stops = [f"S{i}" for i in range(MAX_MERCHANTS)]
    mids = [f"m-{s}" for s in stops]
    data = {
        "depot": "D", "vehicle_capacity": MAX_CAPACITY,
        "merchants": [{"merchant_id": mids[i], "stop": stops[i], "units": 1, "order_id": f"o-{i}"}
                      for i in range(MAX_MERCHANTS)],
        "distance": {"D": {s: 5 for s in stops}},
    }
    for s in stops:
        data["distance"][s] = {"D": 5, **{o: 1 for o in stops if o != s}}
    network = RouteNetwork(data)
    costs = network.all_coalition_costs()
    assert len(costs) == 1 << MAX_MERCHANTS
    grand = tuple(mids)
    fleet = costs[grand]
    served = sorted(member for trip in fleet["trips"] for member in trip["members"])
    assert served == sorted(grand)
    assert all(len(trip["members"]) <= MAX_CAPACITY for trip in fleet["trips"])
    assert costs[()]["cost"] == 0


def test_legacy_solo_preview_still_uses_verified_rows(tmp_path):
    from lakecommit.freight_intake import verified_solo_quotes

    store, schemas = make_store(tmp_path / "legacy")
    quotes = verified_solo_quotes(store, FIXTURES, "query-root", schemas,
                                  json.loads(FIXTURE.read_text(encoding="utf-8")))
    assert [q["amount"] for q in quotes] == ["9", "11", "10.00"]
    assert [q["solo_cost"] for q in quotes] == [20, 18, 16]
    assert sum(q["solo_cost"] for q in quotes) == 54
