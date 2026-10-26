"""Local service simulator. No real orders, bookings, charges, or network calls."""
from __future__ import annotations

from copy import deepcopy
from datetime import date

from .models import Action


class CatalogSandbox:
    def __init__(self, catalog: dict[str, dict]):
        self.catalog = deepcopy(catalog)
        self.executions: list[dict] = []
        self._receipts: dict[str, dict] = {}

    def resolve(self, action: Action) -> dict:
        key = {"order_food": "product_id", "book_hotel": "hotel_id", "book_restaurant": "restaurant_id"}.get(action.tool)
        if key is None:
            raise ValueError("Unknown sandbox operation")
        entity = self.catalog.get(action.arguments.get(key))
        if entity is None:
            return {"_evidence_id": "catalog:missing", "available": False, "total_price": 0}
        if entity.get("operation") != action.tool:
            raise ValueError("Entity does not support the proposed operation")
        facts = deepcopy(entity)
        multiplier = 1
        if action.tool == "order_food":
            multiplier = action.arguments.get("quantity", 1)
            if isinstance(multiplier, bool) or not isinstance(multiplier, int) or multiplier <= 0:
                raise ValueError("Invalid quantity")
        if action.tool == "book_hotel":
            start = date.fromisoformat(action.arguments["check_in"])
            end = date.fromisoformat(action.arguments["check_out"])
            multiplier = max(0, (end - start).days)
        facts["total_price"] = facts["unit_price"] * multiplier
        facts["_evidence_id"] = "catalog:" + str(action.arguments[key])
        return facts

    def execute(self, action: Action) -> dict:
        if action.idempotency_key in self._receipts:
            return deepcopy(self._receipts[action.idempotency_key])
        receipt = {"status": "confirmed", "receipt_id": f"sim-{len(self.executions) + 1}",
                   "tool": action.tool, "arguments": deepcopy(action.arguments), "simulated": True}
        self.executions.append(action.to_dict())
        self._receipts[action.idempotency_key] = receipt
        return deepcopy(receipt)


def example_catalog() -> dict:
    return {
        "peanut-noodles": {"operation": "order_food", "unit_price": 28, "allergens": ["peanut"], "cuisine": "Chinese", "available": True},
        "rice-bowl": {"operation": "order_food", "unit_price": 22, "allergens": [], "cuisine": "Chinese", "available": True},
        "premium-salad": {"operation": "order_food", "unit_price": 65, "allergens": [], "cuisine": "Western", "available": True},
        "hotel-central": {"operation": "book_hotel", "unit_price": 280, "available": True, "city": "Shanghai", "capacity": 2},
        "bistro": {"operation": "book_restaurant", "unit_price": 0, "available": True, "capacity": 4, "cuisine": "Chinese"},
    }
