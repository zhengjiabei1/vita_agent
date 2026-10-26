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
        from vita_agent.official_facts import OfficialObservedFacts, get_official_specs, ignores_purchase_preferences
        from vita.data_model.tasks import Location, Order
        from vita.domains.ota.data_model import Flight, FlightProduct, Train, TrainProduct, Attraction, AttractionProduct
        from vita.domains.instore.data_model import Shop, ShopProduct, BookInfo, ReservationInfo
from vita_agent.guards import ConstraintChecker
from vita_agent.models import Action


def travel(kind, prices=(100, 120), quantities=(3, 2)):
    loc = Location(address="Station", longitude=121, latitude=31)
    product_class = {"flight": FlightProduct, "train": TrainProduct, "attraction": AttractionProduct}[kind]
    category = "ticket_type" if kind == "attraction" else "seat_type"
    products = [product_class(product_id=f"p{i}", date=f"2026-11-0{i+1}", price=p, quantity=q, **{category: "standard"}) for i, (p, q) in enumerate(zip(prices, quantities))]
    if kind == "attraction":
        entity = Attraction(attraction_id="a1", attraction_name="Park", location=loc, description="Park", score=4, opening_hours="09:00-18:00", ticket_price=100, products=products)
    else:
        fields = {f"{kind}_id": "entity1", f"{kind}_number": "A1", "departure_city": "A", "arrival_city": "B", "departure_time": "08:00", "arrival_time": "10:00", "tags": [], "products": products}
        place = "airport" if kind == "flight" else "station"
        fields[f"departure_{place}_location"] = loc
        fields[f"arrival_{place}_location"] = loc
        entity = (Flight if kind == "flight" else Train)(**fields)
    return entity, products


@pytest.mark.parametrize("kind", ["flight", "train", "attraction"])
def test_real_repr_grounding_multiplies_quantity_and_binds_date(kind):
    entity, _ = travel(kind)
    facts = OfficialObservedFacts()
    facts.observe(f"get_ota_{kind}_info", f"{kind.title()} Info:\n{entity!r}", "read1")
    entity_id = "a1" if kind == "attraction" else "entity1"
    product_arg = "ticket_id" if kind == "attraction" else "seat_id"
    args = {f"{kind}_id": entity_id, product_arg: "p0", "date": "2026-11-01", "quantity": 2, "user_id": "u1", "total_price": 1}
    action = Action(f"create_{kind}_order", args, "request1")
    assert facts.resolve(action)["total_price"] == 200
    assert facts.resolve(action)["available"] is True
    assert ConstraintChecker(get_official_specs()).check(action, [], facts.resolve(action)) == []
    assert facts.resolve(Action(action.tool, {**args, "date": "2026-12-01"}, "x")) == {}
    assert facts.resolve(Action(action.tool, {**args, "quantity": True}, "x")) == {}
    assert facts.resolve(Action(action.tool, {**args, f"{kind}_id": "other"}, "x")) == {}
    over = Action(action.tool, {**args, "quantity": 5}, "x")
    assert facts.resolve(over)["available"] is False


def test_untrusted_source_and_malformed_inventory_do_not_create_facts():
    entity, _ = travel("train")
    content = f"Train Info:\n{entity!r}"
    facts = OfficialObservedFacts()
    facts.observe("assistant", content, "fake")
    assert not facts.travel
    facts.observe("get_ota_train_info", content.replace("price=100.0", "price=nan"), "bad")
    assert not facts.travel
    facts.observe("get_ota_train_info", content, "error", error=True)
    assert not facts.travel


def shop():
    product = ShopProduct(product_id="meal", price=25, quantity=5, name="Meal", shop_id="s1", tags=[])
    entity = Shop(shop_id="s1", shop_name="Bistro", score=4, location=Location(address="A", longitude=1, latitude=1), tags=[], enable_book=True, book_price=10, enable_reservation=True, products=[product])
    return entity, product


def test_instore_product_booking_reservation_real_reprs():
    entity, product = shop()
    facts = OfficialObservedFacts()
    facts.observe("instore_shop_search_recommend", str(entity), "shop-read")
    facts.observe("instore_product_search_recommend", repr(product), "product-read")
    action = Action("create_instore_product_order", {"shop_id": "s1", "product_id": "meal", "user_id": "u1", "quantity": 2}, "1")
    assert facts.resolve(action)["total_price"] == 50
    assert facts.resolve(Action(action.tool, {**action.arguments, "shop_id": "s2"}, "2")) == {}
    for tool, price in (("instore_book", 10), ("instore_reservation", 0)):
        candidate = Action(tool, {"shop_id": "s1", "time": "2026-11-01 12:00:00", "user_id": "u1", "customer_count": 2}, tool)
        resolved = facts.resolve(candidate)
        assert resolved["total_price"] == price
        assert resolved["available"] is True
        assert "capacity" not in resolved  # Upstream exposes no table capacity.
        assert not ConstraintChecker(get_official_specs()).check(candidate, [], resolved)


@pytest.mark.parametrize("kind", ["delivery", "instore", "hotel", "flight", "train", "attraction"])
def test_observed_order_payment_ownership_and_cancellation(kind):
    order = Order(order_id="o1", order_type=kind, user_id="owner", store_id="s1", total_price=99, status="unpaid")
    facts = OfficialObservedFacts()
    read = "get_instore_orders" if kind == "instore" else f"get_{kind}_order_detail"
    facts.observe(read, repr(order), "read-order")
    payment = Action(f"pay_{kind}_order", {"order_id": "o1"}, "pay")
    assert facts.resolve(payment)["user_id"] == "owner"
    assert facts.resolve(payment)["available"] is True
    wrong_type = "flight" if kind != "flight" else "train"
    assert facts.resolve(Action(f"pay_{wrong_type}_order", {"order_id": "o1"}, "wrong")) == {}
    cancellation = "instore_cancel_order" if kind == "instore" else f"cancel_{kind}_order"
    assert ignores_purchase_preferences(cancellation)
    assert facts.resolve(Action(cancellation, {"order_id": "o1", "user_id": "intruder"}, "bad")) == {}
    facts.observe(payment.tool, "Payment successful", "pay-result")
    assert facts.resolve(payment) == {}  # Need a fresh observed state.
    order.status = "paid"
    facts.observe(read, repr(order), "paid-read")
    assert facts.resolve(payment)["available"] is False
    assert facts.resolve(Action(cancellation, {"order_id": "o1", "user_id": "owner"}, "cancel"))["available"] is True


def test_booking_records_ownership_status_and_modify():
    facts = OfficialObservedFacts()
    book = BookInfo(book_id="b1", shop_id="s1", book_time="2026-11-01 12:00:00", update_time="2026-10-01 12:00:00", customer_id="u1", customer_count=2, book_price=15, status="unpaid")
    reservation = ReservationInfo(reservation_id="r1", shop_id="s1", reservation_time="2026-11-01 12:00:00", update_time="2026-10-01 12:00:00", customer_id="u1", customer_count=2, status="unconsumed")
    facts.observe("get_instore_books", repr(book), "books-read")
    facts.observe("get_instore_reservations", repr(reservation), "reservations-read")
    assert facts.resolve(Action("pay_instore_book", {"book_id": "b1"}, "pay"))["total_price"] == 15
    modification = Action("instore_modify_reservation", {"reservation_id": "r1", "time": "2026-11-02 12:00:00", "customer_count": 3}, "modify")
    assert facts.resolve(modification)["user_id"] == "u1"
    assert not ConstraintChecker(get_official_specs()).check(modification, [], facts.resolve(modification))
    reservation.status = "cancelled"
    facts.observe("get_instore_reservations", repr(reservation), "read-cancelled")
    assert facts.resolve(modification)["available"] is False


@pytest.mark.parametrize("kind", ["train", "flight"])
def test_modify_travel_requires_paid_single_product_and_new_inventory(kind):
    entity, products = travel(kind)
    old = products[0].model_copy(update={"quantity": 2})
    order = Order(order_id="o1", order_type=kind, user_id="u1", store_id="entity1", total_price=200, status="paid", products=[old])
    facts = OfficialObservedFacts()
    facts.observe(f"get_{kind}_order_detail", repr(order), "order-read")
    action = Action(f"modify_{kind}_order", {"order_id": "o1", "user_id": "u1", "new_date": "2026-11-02"}, "modify")
    assert facts.resolve(action) == {}
    facts.observe(f"get_ota_{kind}_info", f"{kind.title()} Info:\n{entity!r}", "inventory-read")
    assert facts.resolve(action)["total_price"] == 240
    assert facts.resolve(action)["available"] is True
    assert facts.resolve(Action(action.tool, {**action.arguments, "user_id": "other"}, "bad")) == {}
    order.status = "unpaid"
    facts.observe(f"get_{kind}_order_detail", repr(order), "unpaid-read")
    assert facts.resolve(action) == {}


def test_failed_write_invalidates_inventory_and_order_cache():
    entity, _ = travel("train")
    facts = OfficialObservedFacts()
    facts.observe("get_ota_train_info", f"Train Info:\n{entity!r}", "read")
    assert facts.travel
    facts.observe("create_train_order", "timeout", "result", error=True)
    assert not facts.travel and not facts.orders


def test_taxi_upstream_repr_bug_requires_explicit_runner_patch():
    assert "create_taxi_order" in get_official_specs()
    order = Order(order_id="o1", order_type="taxi", user_id="u1", store_id="taxi1")
    with pytest.raises(KeyError):
        repr(order)
