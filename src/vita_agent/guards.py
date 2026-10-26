"""Training-free checks before executing a proposed service tool call.

``facts`` must come from the trusted catalog/tool-result resolver. In particular,
do not merge ``action.arguments`` or model-generated metadata into these facts.
The ``_evidence_id`` field records provenance but is not a cryptographic proof;
the harness is responsible for keeping this trust boundary intact and ensuring
the evidence describes the same candidate, quantities and dates as the action.

This module validates explicit requirements, not every possible business rule.
Callers supply applicable active preferences, verify user authorization and
ownership, and implement tool-specific rules in the resolver or an adapter.
Unknown tools fail closed; additional tools require an explicit ``ToolSpec``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import math
import re
from typing import Any, Iterable, Mapping

from .models import Action, Preference, Violation


@dataclass(frozen=True)
class ToolSpec:
    """Explicit validation policy for a known tool.

    Read-only tools still validate required arguments, quantities and dates, but
    skip commit evidence and preference checks. ``quantity_fields`` accepts a
    positive integer; ``product_cnts`` and ``quantity_list_fields`` accept a
    nonempty list of positive integers.
    ``date_fields`` accepts ISO dates or ISO datetimes. Domain is informational;
    callers must retrieve/filter preferences for the correct user and domain.
    """

    required_args: tuple[str, ...] = ()
    read_only: bool = False
    domain: str = ""
    quantity_fields: tuple[str, ...] = ()
    date_fields: tuple[str, ...] = ()
    requires_price: bool = True
    requires_availability: bool = True
    quantity_list_fields: tuple[str, ...] = ()


DEFAULT_TOOL_SPECS: dict[str, ToolSpec] = {
    "order_food": ToolSpec(("product_id", "quantity"), domain="delivery", quantity_fields=("quantity",)),
    "book_hotel": ToolSpec(
        ("hotel_id", "check_in", "check_out", "guests"), domain="ota",
        quantity_fields=("guests",), date_fields=("check_in", "check_out"),
    ),
    "book_restaurant": ToolSpec(
        ("restaurant_id", "date", "party_size"), domain="instore",
        quantity_fields=("party_size",), date_fields=("date",),
    ),
}

_SLOT_ALIASES = {
    "budget": "total_price",
    "max_budget": "total_price",
    "allergy": "allergens",
    "allergies": "allergens",
}
_OPERATORS = {"eq", "lte", "gte", "excludes", "includes"}
_QUANTITY_FIELDS = {"quantity", "count", "party_size", "guests", "customer_count", "product_cnts", "rooms"}


def _number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or (
        isinstance(value, (list, tuple, dict)) and not value
    )


def _parse_date(value: Any) -> date | datetime | None:
    if not isinstance(value, str):
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    if not re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", value):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _atom(value: Any) -> tuple[str, Any]:
    """Typed canonical scalar; reject structures instead of guessing semantics."""
    if isinstance(value, str) and value.strip():
        return ("text", value.strip().casefold())
    if isinstance(value, bool):
        return ("bool", value)
    if _number(value):
        return ("number", value)
    raise ValueError("Expected a nonempty string, boolean, or finite number")


def _values(value: Any) -> set[tuple[str, Any]]:
    if isinstance(value, (list, tuple, set, frozenset)):
        return {_atom(item) for item in value}
    return {_atom(value)}


def _satisfies(actual: Any, expected: Any, operator: str) -> bool:
    if operator in {"lte", "gte"}:
        if not _number(actual) or not _number(expected):
            raise ValueError("Numeric constraints require finite numbers, excluding booleans")
        return actual <= expected if operator == "lte" else actual >= expected
    if operator == "eq":
        return _values(actual) == _values(expected)
    actual_set, expected_set = _values(actual), _values(expected)
    if operator == "includes":
        return expected_set <= actual_set
    return actual_set.isdisjoint(expected_set)


class ConstraintChecker:
    """Validate a known proposal using trusted candidate evidence and preferences."""

    def __init__(self, tool_specs: Mapping[str, ToolSpec] | None = None, *,
                 unknown_policy: str = "strict") -> None:
        if unknown_policy not in ("strict", "warn"):
            raise ValueError("unknown_policy must be strict or warn")
        self.unknown_policy = unknown_policy
        self.tool_specs = dict(DEFAULT_TOOL_SPECS)
        if tool_specs is not None:
            for name, spec in tool_specs.items():
                if not isinstance(name, str) or not name.strip() or not isinstance(spec, ToolSpec):
                    raise ValueError("tool_specs must map nonempty tool names to ToolSpec records")
                self.tool_specs[name] = spec

    def check(self, action: Action, preferences: Iterable[Preference], facts: dict) -> list[Violation]:
        violations: list[Violation] = []
        if not isinstance(action.tool, str) or action.tool not in self.tool_specs:
            return [Violation("unsupported_tool", "No explicit validation policy is registered for this tool")]
        spec = self.tool_specs[action.tool]
        if not isinstance(action.arguments, dict):
            return [Violation("invalid_argument", "Tool arguments must be an object")]
        args = action.arguments
        for name in spec.required_args:
            if name not in args or _missing(args[name]):
                violations.append(Violation("missing_argument", f"Required argument '{name}' is missing", name))
            elif name.endswith("_id") and not isinstance(args[name], str):
                violations.append(Violation("invalid_argument", f"'{name}' must be a nonempty string", name))

        for name in sorted(_QUANTITY_FIELDS | set(spec.quantity_fields) | set(spec.quantity_list_fields)):
            if name not in args:
                continue
            value = args[name]
            if name == "product_cnts" or name in spec.quantity_list_fields:
                valid = isinstance(value, (list, tuple)) and bool(value) and all(_positive_int(item) for item in value)
            else:
                valid = _positive_int(value)
            if not valid:
                violations.append(Violation("invalid_argument", f"'{name}' must contain positive integer quantities", name))

        if "product_ids" in args:
            products = args["product_ids"]
            if not isinstance(products, (list, tuple)) or not products or not all(isinstance(item, str) and item.strip() for item in products):
                violations.append(Violation("invalid_argument", "product_ids must be a nonempty list of identifiers", "product_ids"))
            elif isinstance(args.get("product_cnts"), (list, tuple)) and len(products) != len(args["product_cnts"]):
                violations.append(Violation("invalid_argument", "product_ids and product_cnts must have the same length", "product_cnts"))

        parsed_dates: dict[str, date | datetime] = {}
        for name in sorted(set(spec.date_fields) | {"check_in", "check_out"}):
            if name not in args:
                continue
            parsed = _parse_date(args[name])
            if parsed is None:
                violations.append(Violation("invalid_argument", f"'{name}' must be a valid ISO date or datetime", name))
            else:
                parsed_dates[name] = parsed
        if "check_in" in args or "check_out" in args:
            for name in ("check_in", "check_out"):
                if name not in args and name not in spec.required_args:
                    violations.append(Violation("missing_argument", "Both check_in and check_out are required", name))
            if "check_in" in parsed_dates and "check_out" in parsed_dates:
                try:
                    valid_range = parsed_dates["check_out"] > parsed_dates["check_in"]
                except TypeError:
                    valid_range = False
                if not valid_range:
                    violations.append(Violation("invalid_date_range", "check_out must be later than check_in with matching date/time formats", "check_out"))

        if spec.read_only:
            return violations

        if not isinstance(action.idempotency_key, str) or not action.idempotency_key.strip():
            violations.append(Violation("missing_argument", "Mutating actions require an idempotency key", "idempotency_key"))
        if not isinstance(facts, dict):
            if self.unknown_policy == "strict":
                violations.append(Violation("missing_evidence", "Trusted candidate evidence must be an object"))
            return violations
        evidence_id = facts.get("_evidence_id")
        if not isinstance(evidence_id, str) or not evidence_id.strip():
            if self.unknown_policy == "strict":
                violations.append(Violation("missing_evidence", "Trusted candidate evidence needs a provenance identifier", "_evidence_id"))
        if spec.requires_availability:
            available = facts.get("available")
            if not isinstance(available, bool):
                if self.unknown_policy == "strict":
                    violations.append(Violation("missing_evidence", "A trusted boolean availability observation is required", "available"))
            elif not available:
                violations.append(Violation("unavailable", "The selected candidate is unavailable", "available"))
        if spec.requires_price:
            if "total_price" not in facts or facts["total_price"] is None:
                if self.unknown_policy == "strict":
                    violations.append(Violation("missing_evidence", "A trusted total price for this action is required", "total_price"))
            elif not _number(facts["total_price"]) or facts["total_price"] < 0:
                violations.append(Violation("invalid_evidence", "Total price must be a finite nonnegative number, excluding booleans", "total_price"))
        if "capacity" in facts:
            capacity = facts["capacity"]
            if not _positive_int(capacity):
                violations.append(Violation("invalid_evidence", "Capacity must be a positive integer", "capacity"))
            else:
                for name in ("party_size", "guests", "customer_count"):
                    if name in args and _positive_int(args[name]) and args[name] > capacity:
                        violations.append(Violation("capacity_exceeded", f"'{name}' exceeds trusted capacity", name))

        for preference in preferences:
            if preference.status != "active" or preference.strength != "hard":
                continue
            if not isinstance(preference.slot, str) or not preference.slot:
                violations.append(Violation("invalid_preference", "Hard preference has no valid slot", evidence_id=preference.evidence_id))
                continue
            if not isinstance(preference.operator, str) or preference.operator not in _OPERATORS:
                violations.append(Violation("unsupported_operator", "Hard preference uses an unsupported operator", preference.slot, preference.evidence_id))
                continue
            slot = _SLOT_ALIASES.get(preference.slot, preference.slot)
            if slot not in facts or facts[slot] is None:
                if self.unknown_policy == "strict":
                    violations.append(Violation("missing_evidence", f"No trusted '{slot}' evidence to check the hard preference", preference.slot, preference.evidence_id))
                continue
            try:
                satisfied = _satisfies(facts[slot], preference.value, preference.operator)
            except (ValueError, TypeError, OverflowError):
                violations.append(Violation("invalid_evidence", f"Cannot safely compare '{slot}' evidence with the hard preference", preference.slot, preference.evidence_id))
                continue
            if not satisfied:
                code = "budget_exceeded" if slot == "total_price" and preference.operator == "lte" else "preference_conflict"
                violations.append(Violation(code, f"Trusted '{slot}' evidence violates the hard '{preference.operator}' preference", preference.slot, preference.evidence_id))
        return violations
