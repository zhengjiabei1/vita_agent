from dataclasses import replace
import math

import pytest

from vita_agent.guards import ConstraintChecker, ToolSpec
from vita_agent.models import Action, Preference


def preference(slot="budget", value=50, operator="lte", **kwargs):
    return Preference("p1", "u1", "food", slot, value, "hard", operator, "user-message-1", "At most 50", 1, **kwargs)


def food(**arguments):
    return Action("order_food", {"product_id": "dish-1", "quantity": 1, **arguments}, "action-1")


def evidence(**overrides):
    return {"_evidence_id": "catalog/dish-1", "available": True, "total_price": 20,
            "allergens": [], "cuisine": "Chinese", "city": "Shanghai", **overrides}


def codes(violations):
    return {v.code for v in violations}


def test_valid_order_and_trusted_price_not_model_price():
    checker = ConstraintChecker()
    assert checker.check(food(total_price=1), [preference()], evidence()) == []
    result = checker.check(food(total_price=1), [preference()], evidence(total_price=100))
    assert "budget_exceeded" in codes(result)
    assert result[0].evidence_id == "user-message-1"


def test_model_allergen_claim_cannot_override_trusted_evidence():
    result = ConstraintChecker().check(food(allergens=[]), [preference("allergies", ["peanut"], "excludes")], evidence(allergens=["Peanut"]))
    assert "preference_conflict" in codes(result)


@pytest.mark.parametrize("slot,value,operator", [
    ("city", "Shanghai", "eq"),
    ("cuisine", ["Chinese"], "includes"),
    ("total_price", 10, "gte"),
    ("allergens", ["peanut"], "excludes"),
])
def test_supported_preference_operators(slot, value, operator):
    assert ConstraintChecker().check(food(), [preference(slot, value, operator)], evidence()) == []


def test_warn_policy_fails_open_on_unknown_but_blocks_explicit_violation():
    checker = ConstraintChecker(unknown_policy="warn")
    hard = preference("vegetarian", True, "eq")
    assert checker.check(food(), [hard], {}) == []
    assert checker.check(food(), [], None) == []
    assert "unavailable" in codes(checker.check(food(), [], {"available": False, "total_price": 20}))
    assert "budget_exceeded" in codes(checker.check(food(), [preference()], evidence(total_price=100)))
    with pytest.raises(ValueError, match="unknown_policy"):
        ConstraintChecker(unknown_policy="ignore")


def test_missing_hard_evidence_blocks_but_soft_and_inactive_do_not():
    hard = preference("vegetarian", True, "eq")
    assert "missing_evidence" in codes(ConstraintChecker().check(food(vegetarian=True), [hard], evidence()))
    assert ConstraintChecker().check(food(), [replace(hard, strength="soft"), replace(hard, status="superseded")], evidence()) == []


@pytest.mark.parametrize("facts", [None, {}, {"total_price": 20, "available": True},
                                  {"_evidence_id": "x", "available": "yes", "total_price": 20}])
def test_missing_trusted_metadata_blocks(facts):
    assert "missing_evidence" in codes(ConstraintChecker().check(food(), [], facts))


@pytest.mark.parametrize("quantity", [0, -1, 1.5, True, "1", None, math.nan, [1, 0], [1]])
def test_invalid_quantities_do_not_crash(quantity):
    assert "invalid_argument" in codes(ConstraintChecker().check(food(quantity=quantity), [], evidence()))


@pytest.mark.parametrize("price", [math.nan, math.inf, -1, True, "20", [], {}, 10**1000])
def test_invalid_prices_do_not_crash(price):
    assert "invalid_evidence" in codes(ConstraintChecker().check(food(), [], evidence(total_price=price)))


def test_valid_hotel_and_date_range_validation():
    action = Action("book_hotel", {"hotel_id": "h1", "guests": 2, "check_in": "2026-11-01", "check_out": "2026-11-03"}, "booking-1")
    checker = ConstraintChecker()
    assert checker.check(action, [], evidence(capacity=2)) == []
    invalid = replace(action, arguments={**action.arguments, "check_out": "2026-11-01"})
    assert "invalid_date_range" in codes(checker.check(invalid, [], evidence()))
    invalid = replace(action, arguments={**action.arguments, "check_in": "2026-02-30"})
    assert "invalid_argument" in codes(checker.check(invalid, [], evidence()))
    invalid = replace(action, arguments={**action.arguments, "check_in": []})
    assert "invalid_argument" in codes(checker.check(invalid, [], evidence()))


def test_unavailable_capacity_and_missing_argument():
    action = Action("book_restaurant", {"restaurant_id": "r1", "date": "2026-11-03", "party_size": 8}, "booking-2")
    result = ConstraintChecker().check(action, [], evidence(capacity=4, available=False))
    assert {"capacity_exceeded", "unavailable"} <= codes(result)
    result = ConstraintChecker().check(replace(action, arguments={}), [], evidence())
    assert "missing_argument" in codes(result)


def test_unknown_tool_and_operator_fail_closed():
    assert codes(ConstraintChecker().check(Action("pay_anything", {}, "x"), [], evidence())) == {"unsupported_tool"}
    assert "unsupported_operator" in codes(ConstraintChecker().check(food(), [preference(operator="similar")], evidence()))


def test_malformed_preference_values_cannot_crash_comparison():
    malformed = [preference(value=True), preference("allergens", {"nested": []}, "excludes")]
    assert "invalid_evidence" in codes(ConstraintChecker().check(food(), malformed, evidence()))


def test_explicit_read_only_tool_needs_args_but_no_commit_facts():
    checker = ConstraintChecker({"search_food": ToolSpec(("query",), read_only=True)})
    assert checker.check(Action("search_food", {"query": "rice"}, ""), [preference()], {}) == []
    assert "missing_argument" in codes(checker.check(Action("search_food", {}, ""), [], {}))


def test_adapter_can_declare_upstream_list_quantities():
    checker = ConstraintChecker({"create_delivery_order": ToolSpec(("product_ids", "product_cnts"), quantity_fields=("product_cnts",))})
    action = Action("create_delivery_order", {"product_ids": ["dish-1"], "product_cnts": [2]}, "upstream-1")
    assert checker.check(action, [], evidence()) == []
    assert "invalid_argument" in codes(checker.check(replace(action, arguments={**action.arguments, "product_cnts": [True]}), [], evidence()))
    assert "invalid_argument" in codes(checker.check(replace(action, arguments={**action.arguments, "product_cnts": [1, 2]}), [], evidence()))


def test_commit_needs_idempotency_key():
    assert "missing_argument" in codes(ConstraintChecker().check(replace(food(), idempotency_key=""), [], evidence()))


def test_boolean_never_equals_numeric_preference():
    result = ConstraintChecker().check(food(), [preference("verified", True, "eq")], evidence(verified=1))
    assert "preference_conflict" in codes(result)
