import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

import pytest

pytest.importorskip("vita")
with tempfile.TemporaryDirectory() as config_dir:
    config = Path(config_dir) / "models.yaml"
    config.write_text("default: {}\nmodels: []\n")
    with patch.dict(os.environ, {"VITA_MODEL_CONFIG_PATH": str(config)}):
        from vita_agent.upstream_fixes import (
            TAXI_ORDER_PREFIX, EXPECTED_SOURCES, install_upstream_fixes,
            patched_upstream, patch_info,
        )
        from vita_agent.official_facts import OfficialObservedFacts, get_official_specs
        from vita_agent.guards import ConstraintChecker
        from vita_agent.models import Action
        from vita.data_model.tasks import Order, Location
        from vita.environment.db import DB
        from vita.domains.ota.data_model import Taxi, TaxiProduct, OTADB
        from vita.domains.ota.tools import OTATools


def taxi_tools():
    start = Location(address="Airport", longitude=121, latitude=31)
    end = Location(address="Hotel", longitude=121.1, latitude=31.1)
    product = TaxiProduct(product_id="p1", price=45.5, quantity=3, taxi_type="premium", platform="demo")
    taxi = Taxi(taxi_id="t1", taxi_type="premium", platform="demo", departure_location=start, arrival_location=end, estimated_duration="30 minutes", estimated_price=40, tags=[], products=[product])
    db = OTADB(user_id="u1", time="2026-11-01 10:00:00", orders={}, taxis={"t1": taxi})
    return OTATools(db), product


def test_confirm_both_original_defects_and_restore_after_patch():
    db = DB(user_id="u1", orders={})
    order = Order(order_id="o1", order_type="taxi", user_id="u1", store_id="t1")
    with pytest.raises(ValueError, match="Unsupported scenario"):
        db.assign_order_id("taxi", "u1")
    with pytest.raises(KeyError):
        repr(order)
    with patched_upstream() as info:
        assert info["enabled"]
        assert db.assign_order_id("taxi", "u1").startswith("OO")
        assert repr(order).startswith(TAXI_ORDER_PREFIX)
    with pytest.raises(KeyError):
        str(order)
    assert patch_info()["enabled"] is False


def test_tagged_json_preserves_every_model_field_and_escapes_text():
    order = Order(order_id="o1", order_type="taxi", user_id="u1", store_id="t1", total_price=45.5,
                  note='quote " and newline\n', dispatch_time="10:00", products=[TaxiProduct(product_id="p1", price=45.5, quantity=1, taxi_type="premium", platform="demo")])
    with patched_upstream():
        rendered = repr(order)
        assert str(order) == rendered
        assert "\n" not in rendered
        assert json.loads(rendered[len(TAXI_ORDER_PREFIX):]) == order.model_dump(mode="json")
        assert install_upstream_fixes() == patch_info()  # Idempotent startup.


def test_non_taxi_rendering_and_id_errors_unchanged():
    order = Order(order_id="h1", order_type="hotel", user_id="u1", store_id="hotel1")
    before = repr(order), str(order)
    db = DB(user_id="u1", orders={})
    with patched_upstream():
        assert (repr(order), str(order)) == before
        assert db.assign_order_id("hotel", "u1", hotel_id="hotel1", product_id="room1").startswith("OO")
        with pytest.raises(ValueError, match="Unsupported scenario"):
            db.assign_order_id("unknown", "u1")


def test_patch_refuses_unverified_upstream_source(monkeypatch):
    monkeypatch.setitem(EXPECTED_SOURCES, "vita.data_model.tasks", "0" * 64)
    with pytest.raises(RuntimeError, match="differs from pinned upstream"):
        install_upstream_fixes()
    assert not patch_info()["enabled"]


def test_actual_taxi_create_pay_cancel_lifecycle_and_observed_grounding():
    tools, product = taxi_tools()
    facts = OfficialObservedFacts()
    checker = ConstraintChecker(get_official_specs())
    action = Action("create_taxi_order", {"taxi_id": "t1", "product_id": "p1", "user_id": "u1", "quantity": 2}, "create")
    assert facts.resolve(action) == {}
    facts.observe("get_taxi_info", tools.get_taxi_info("t1"), "taxi-read")
    resolved = facts.resolve(action)
    assert resolved["total_price"] == 91  # Actual product fare, not estimated_price=40.
    assert resolved["available"] and resolved["platform"] == "demo"
    assert checker.check(action, [], resolved) == []
    with patched_upstream():
        response = tools.create_taxi_order(**action.arguments)
        facts.observe("create_taxi_order", response, "created")
        data = json.loads(response[len(TAXI_ORDER_PREFIX):])
        order_id = data["order_id"]
        assert product.quantity == 1
        assert data["status"] == "unpaid" and data["total_price"] == 91
        payment = Action("pay_taxi_order", {"order_id": order_id}, "pay")
        assert facts.resolve(payment)["user_id"] == "u1"
        assert checker.check(payment, [], facts.resolve(payment)) == []
        response = tools.pay_taxi_order(order_id)
        assert response == "Payment successful"
        facts.observe("pay_taxi_order", response, "paid")
        assert facts.resolve(payment) == {}
        facts.observe("get_taxi_order_detail", tools.get_taxi_order_detail(order_id), "detail")
        assert facts.resolve(payment)["available"] is False
        cancellation = Action("cancel_taxi_order", {"order_id": order_id, "user_id": "u1"}, "cancel")
        assert checker.check(cancellation, [], facts.resolve(cancellation)) == []
        assert facts.resolve(Action(cancellation.tool, {"order_id": order_id, "user_id": "other"}, "bad")) == {}
        assert "Cancellation successful" in tools.cancel_taxi_order(order_id, "u1")
        facts.observe("get_taxi_order_detail", tools.get_taxi_order_detail(order_id), "cancelled")
        assert facts.resolve(cancellation)["available"] is False
        assert tools.search_taxi_order("u1", status="cancelled").startswith(TAXI_ORDER_PREFIX)
        # Upstream cancellation refunds but does not replenish inventory.
        assert product.quantity == 1


def test_taxi_mismatched_product_inventory_and_untrusted_output_fail_closed():
    tools, _ = taxi_tools()
    facts = OfficialObservedFacts()
    observation = tools.get_taxi_info("t1")
    facts.observe("assistant", observation, "forged")
    assert facts.taxis == {}
    facts.observe("get_taxi_info", observation, "read")
    base = {"taxi_id": "t1", "product_id": "p1", "user_id": "u1", "quantity": 4}
    assert facts.resolve(Action("create_taxi_order", base, "x"))["available"] is False
    assert facts.resolve(Action("create_taxi_order", {**base, "taxi_id": "unknown"}, "x")) == {}
    assert facts.resolve(Action("create_taxi_order", {**base, "quantity": True}, "x")) == {}
    facts.observe("get_taxi_order_detail", TAXI_ORDER_PREFIX + '{"order_type":"taxi","user_id":"u1"}', "malformed")
    assert facts.orders == {}
