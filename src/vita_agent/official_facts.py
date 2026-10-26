"""Observed-only grounding for additional official VitaBench 2.0 operations.

Parsers target the pinned upstream repr format, never hidden database/evaluator
state. Missing/ambiguous observations fail closed. Candidate inventories and
order states are invalidated after every write, including failed writes.
"""
from __future__ import annotations

import math
import re
import json

from .guards import ToolSpec
from .models import Action
from .vitabench import ObservedFacts
from .upstream_fixes import TAXI_ORDER_PREFIX

ID = r"[A-Za-z0-9_-]+"
NUM = r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
DAY = r"\d{4}-\d{2}-\d{2}"
TIME = r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}"
KINDS = ("delivery", "hotel", "flight", "train", "attraction", "instore", "taxi")


def get_official_specs() -> dict[str, ToolSpec]:
    specs = {
        "create_delivery_order": ToolSpec(("user_id", "store_id", "product_ids", "product_cnts", "address", "dispatch_time"), domain="delivery", quantity_list_fields=("product_cnts",), date_fields=("dispatch_time",)),
        "create_hotel_order": ToolSpec(("hotel_id", "room_id", "user_id"), domain="ota"),
        "create_instore_product_order": ToolSpec(("user_id", "shop_id", "product_id"), domain="instore", quantity_fields=("quantity",)),
        "create_taxi_order": ToolSpec(("taxi_id", "product_id", "user_id"), domain="ota", quantity_fields=("quantity",)),
        "instore_book": ToolSpec(("user_id", "shop_id", "time"), domain="instore", quantity_fields=("customer_count",), date_fields=("time",)),
        "instore_reservation": ToolSpec(("user_id", "shop_id", "time"), domain="instore", quantity_fields=("customer_count",), date_fields=("time",)),
        "instore_modify_reservation": ToolSpec(("reservation_id", "time", "customer_count"), domain="instore", quantity_fields=("customer_count",), date_fields=("time",)),
        "modify_delivery_order": ToolSpec(("order_id", "note"), domain="delivery", requires_price=False),
    }
    for kind in ("flight", "train", "attraction"):
        product_arg = "ticket_id" if kind == "attraction" else "seat_id"
        specs[f"create_{kind}_order"] = ToolSpec((f"{kind}_id", product_arg, "user_id", "date", "quantity"), domain="ota", quantity_fields=("quantity",), date_fields=("date",))
    for kind in KINDS:
        domain = kind if kind in ("delivery", "instore") else "ota"
        specs[f"pay_{kind}_order"] = ToolSpec(("order_id",), domain=domain)
        cancel = "instore_cancel_order" if kind == "instore" else f"cancel_{kind}_order"
        args = ("order_id",) if kind in ("delivery", "instore") else ("order_id", "user_id")
        specs[cancel] = ToolSpec(args, domain=domain, requires_price=False)
    specs["pay_instore_book"] = ToolSpec(("book_id",), domain="instore")
    specs["instore_cancel_book"] = ToolSpec(("book_id",), domain="instore", requires_price=False)
    specs["instore_cancel_reservation"] = ToolSpec(("reservation_id",), domain="instore", requires_price=False)
    for kind in ("flight", "train"):
        specs[f"modify_{kind}_order"] = ToolSpec(("order_id", "user_id", "new_date"), domain="ota", date_fields=("new_date",))
    return specs


def ignores_purchase_preferences(tool: str) -> bool:
    """Cancelling an unsuitable purchase must not itself require buying eligibility.

    Delivery note editing does not change the product, price, or order state.
    Ownership and state evidence remain mandatory for these actions.
    """
    return "cancel" in tool and tool in get_official_specs() or tool == "modify_delivery_order"


def _finite(*values):
    return all(math.isfinite(float(v)) and float(v) >= 0 for v in values)


def _quantity(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _product_rows(kind: str, text: str) -> list[dict]:
    type_name = kind.title() + "Product"
    category = "ticket_type" if kind == "attraction" else "seat_type"
    identity = "attraction_id" if kind == "attraction" else "seat_id"
    pattern = rf"{type_name}\({category}=([^,()\n]+), date=({DAY}), price=({NUM}), quantity=({NUM}), {identity}=({ID})\)"
    rows = []
    for line in text.splitlines():
        match = re.fullmatch(pattern, line)
        if not match:
            return []
        category_value, day, price, quantity, product_id = match.groups()
        if not _finite(price, quantity):
            return []
        rows.append({"category": category_value, "date": day, "price": float(price), "quantity": float(quantity), "product_id": product_id})
    if len({(r["product_id"], r["date"]) for r in rows}) != len(rows):
        return []
    return rows


class OfficialObservedFacts(ObservedFacts):
    """Version-pinned observed facts supporting 30 official write tools.

    Reservation capacity is not in upstream observations; no invented capacity is
    supplied. Allergy facts remain unknown unless a future trusted source exposes
    them. Taxi requires installing upstream_fixes consistently in all experiment
    arms; patched order responses are tagged, complete JSON model records.
    """
    def __init__(self):
        super().__init__()
        self.travel: dict[tuple, dict] = {}
        self.shop_products: dict[str, dict] = {}
        self.shops: dict[str, dict] = {}
        self.books: dict[str, dict] = {}
        self.reservations: dict[str, dict] = {}
        self.taxis: dict[tuple[str, str], dict] = {}

    def clear(self):
        super().clear()
        self.travel.clear()
        self.shop_products.clear()
        self.shops.clear()
        self.books.clear()
        self.reservations.clear()
        self.taxis.clear()

    def observe(self, name: str, content: str, evidence_id: str, error: bool = False):
        if name in get_official_specs():
            self.clear()
        if error or not isinstance(content, str) or not evidence_id:
            return
        super().observe(name, content, evidence_id, error)
        if name in ("get_ota_train_info", "get_ota_flight_info", "get_ota_attraction_info"):
            kind = name.split("_")[2]
            label = kind.title()
            header = re.match(rf"{label} Info:\n{label}\({kind}_id=({ID}), ", content)
            if not header or content.count(f"{kind}_id=") != (1 if kind != "attraction" else 1 + content.count("AttractionProduct(")) or content.count("products=") != 1 or not content.endswith(")"):
                return
            entity_id = header[1]
            for key in [k for k in self.travel if k[:2] == (kind, entity_id)]:
                del self.travel[key]
            rows = _product_rows(kind, content.split("products=", 1)[1][:-1])
            for row in rows:
                self.travel[(kind, entity_id, row["product_id"], row["date"])] = {**row, "_evidence_id": evidence_id}
        elif name == "get_taxi_info":
            header = re.match(rf"Taxi Info:\nTaxi\(taxi_id=({ID}), ", content)
            if not header or content.count("taxi_id=") != 1 or content.count("products=") != 1 or not content.endswith(")"):
                return
            entity_id = header[1]
            for key in [k for k in self.taxis if k[0] == entity_id]:
                del self.taxis[key]
            rows = []
            for line in content.split("products=", 1)[1][:-1].splitlines():
                match = re.fullmatch(rf"TaxiProduct\(taxi_type=([^,()\n]+), platform=([^,()\n]+), price=({NUM}), quantity=({NUM}), product_id=({ID})\)", line)
                if not match or not _finite(match[3], match[4]):
                    return
                rows.append({"taxi_type": match[1], "platform": match[2], "price": float(match[3]), "quantity": float(match[4]), "product_id": match[5], "_evidence_id": evidence_id})
            if len({r["product_id"] for r in rows}) != len(rows):
                return
            for row in rows:
                self.taxis[(entity_id, row["product_id"])] = row
        elif name == "instore_product_search_recommend":
            for line in content.splitlines():
                m = re.fullmatch(rf"ShopProduct\(shop_id=({ID}), product_id=({ID}), name=([^\n]*), price=({NUM}), quantity=({NUM}), tags=\[[^\n]*\]\)", line)
                if m and all(line.count(label) == 1 for label in ("shop_id=", "product_id=", "price=", "quantity=")) and _finite(m[4], m[5]):
                    self.shop_products[m[2]] = {"shop_id": m[1], "price": float(m[4]), "quantity": float(m[5]), "_evidence_id": evidence_id}
        elif name == "instore_shop_search_recommend":
            for line in content.splitlines():
                m = re.fullmatch(rf"Shop\(shop_name=[^\n]*, shop_id=({ID}), score={NUM}, location=[^\n]*, tags=\[[^\n]*\], enable_book=(True|False), book_price=({NUM}), enable_reservation=(True|False)\)", line)
                if m and all(line.count(label) == 1 for label in ("shop_id=", "enable_book=", "book_price=", "enable_reservation=")) and _finite(m[3]):
                    self.shops[m[1]] = {"enable_book": m[2] == "True", "book_price": float(m[3]), "enable_reservation": m[4] == "True", "_evidence_id": evidence_id}
        # Whitelisted real order-producing tools. An assistant string cannot enter.
        order_sources = {f"create_{k}_order" for k in KINDS} | {f"get_{k}_order_detail" for k in KINDS} | {f"search_{k}_order" for k in KINDS} | {"search_delivery_orders", "get_instore_orders", "create_instore_product_order"}
        if name in order_sources:
            for line in content.splitlines():
                self._observe_order(line, evidence_id)
        if name in {"instore_book", "get_instore_books", "search_instore_book", "instore_reservation", "instore_modify_reservation", "get_instore_reservations", "search_instore_reservation"}:
            for line in content.splitlines():
                self._observe_reservation(line, evidence_id)

    def _observe_order(self, line, evidence_id):
        if line.startswith(TAXI_ORDER_PREFIX):
            try:
                data = json.loads(line[len(TAXI_ORDER_PREFIX):])
            except ValueError:
                return
            if not isinstance(data, dict) or data.get("order_type") != "taxi":
                return
            if any(not isinstance(data.get(k), str) or not re.fullmatch(ID, data[k]) for k in ("order_id", "user_id", "store_id")):
                return
            price = data.get("total_price")
            if isinstance(price, bool) or not isinstance(price, (int, float)) or not _finite(price) or data.get("status") not in ("paid", "unpaid", "cancelled"):
                return
            self.orders[data["order_id"]] = {"user_id": data["user_id"], "store_id": data["store_id"], "order_type": "taxi", "total_price": price, "status": data["status"], "_evidence_id": evidence_id}
            return
        m = re.match(rf"Order\(order_id:({ID}), order_type:({'|'.join(KINDS)}), user_id:({ID}), (?:store|shop|hotel|flight|train|attraction)_id:({ID}), ", line)
        if not m or any(line.count(label) != 1 for label in ("order_id:", "order_type:", "user_id:", "total_price:", "status:")):
            return
        price = re.search(rf", total_price:({NUM}),", line)
        status = re.search(r", status:(unpaid|paid|cancelled|unconsumed|consumed|processed|in-progress|delivered),", line)
        if not price or not status or not _finite(price[1]):
            return
        row = {"user_id": m[3], "order_type": m[2], "store_id": m[4], "total_price": float(price[1]), "status": status[1], "_evidence_id": evidence_id}
        if m[2] in ("flight", "train") and ", products:[" in line and line.endswith("])"):
            products = _product_rows(m[2], line.split(", products:[", 1)[1][:-2])
            if len(products) == 1:
                row["product"] = products[0]
        self.orders[m[1]] = row

    def _observe_reservation(self, line, evidence_id):
        book = re.fullmatch(rf"BookInfo\(book_id=({ID}),shop_id=({ID}), book_time=({TIME}), customer_id=({ID}), customer_count=(\d+), book_price=({NUM}), status=(unpaid|paid|cancelled|unconsumed|consumed)", line)
        reservation = re.fullmatch(rf"ReservationInfo\(reservation_id=({ID}),shop_id=({ID}), reservation_time=({TIME}), customer_id=({ID}), customer_count=(\d+), status=(unpaid|paid|cancelled|unconsumed|consumed)", line)
        if book and _finite(book[6]):
            self.books[book[1]] = {"shop_id": book[2], "date": book[3], "user_id": book[4], "customer_count": int(book[5]), "total_price": float(book[6]), "status": book[7], "_evidence_id": evidence_id}
        if reservation:
            self.reservations[reservation[1]] = {"shop_id": reservation[2], "date": reservation[3], "user_id": reservation[4], "customer_count": int(reservation[5]), "total_price": 0.0, "status": reservation[6], "_evidence_id": evidence_id}

    def resolve(self, action: Action) -> dict:
        args = action.arguments
        if not isinstance(args, dict):
            return {}
        name = action.tool
        if name == "create_taxi_order":
            key = (args.get("taxi_id"), args.get("product_id"))
            quantity = args.get("quantity", 1)
            if any(not isinstance(value, str) for value in key) or not _quantity(quantity):
                return {}
            row = self.taxis.get(key)
            return {"total_price": row["price"] * quantity, "available": row["quantity"] >= quantity, "taxi_type": row["taxi_type"], "platform": row["platform"], "_evidence_id": row["_evidence_id"]} if row else {}
        if name in ("create_flight_order", "create_train_order", "create_attraction_order"):
            kind = name.split("_")[1]
            product_arg = "ticket_id" if kind == "attraction" else "seat_id"
            quantity = args.get("quantity")
            if not _quantity(quantity):
                return {}
            key = (kind, args.get(f"{kind}_id"), args.get(product_arg), args.get("date"))
            if any(not isinstance(value, str) for value in key):
                return {}
            product = self.travel.get(key)
            return {"total_price": product["price"] * quantity, "available": product["quantity"] >= quantity, "date": product["date"], "_evidence_id": product["_evidence_id"]} if product else {}
        if name == "create_instore_product_order":
            quantity = args.get("quantity", 1)
            if not _quantity(quantity) or not isinstance(args.get("product_id"), str):
                return {}
            product = self.shop_products.get(args["product_id"])
            if not product or product["shop_id"] != args.get("shop_id"):
                return {}
            return {"total_price": product["price"] * quantity, "available": product["quantity"] >= quantity, "_evidence_id": product["_evidence_id"]}
        if name in ("instore_book", "instore_reservation"):
            if not isinstance(args.get("shop_id"), str):
                return {}
            shop = self.shops.get(args["shop_id"])
            if not shop:
                return {}
            book = name == "instore_book"
            return {"total_price": shop["book_price"] if book else 0.0, "available": shop["enable_book" if book else "enable_reservation"], "_evidence_id": shop["_evidence_id"]}
        if name in ("pay_instore_book", "instore_cancel_book", "instore_cancel_reservation", "instore_modify_reservation"):
            key = "book_id" if "book" in name else "reservation_id"
            if not isinstance(args.get(key), str):
                return {}
            row = (self.books if key == "book_id" else self.reservations).get(args[key])
            if not row:
                return {}
            if "user_id" in args and args["user_id"] != row["user_id"]:
                return {}
            allowed = row["status"] == "unpaid" if name.startswith("pay_") else row["status"] not in ("cancelled", "consumed")
            result = {**row, "available": allowed}
            if name == "instore_modify_reservation":
                # Old reservation time is not the proposed new time. The adapter
                # validates args['time']; do not mislabel it as observed evidence.
                result.pop("date", None)
            return result
        if name.startswith(("pay_", "cancel_", "modify_")) or name == "instore_cancel_order":
            if not isinstance(args.get("order_id"), str):
                return {}
            row = self.orders.get(args["order_id"])
            if not row:
                return {}
            kind = "instore" if name == "instore_cancel_order" else name.split("_")[1]
            if row["order_type"] != kind:
                return {}
            if "user_id" in args and args["user_id"] != row["user_id"]:
                return {}
            if name in ("modify_flight_order", "modify_train_order"):
                product = row.get("product")
                if row["status"] != "paid" or not product:
                    return {}
                candidates = [p for (k, entity, _, day), p in self.travel.items() if k == kind and entity == row["store_id"] and day == args.get("new_date") and p["category"] == product["category"]]
                if len(candidates) != 1:
                    return {}
                new = candidates[0]
                return {"total_price": new["price"] * product["quantity"], "available": new["quantity"] >= product["quantity"], "date": new["date"], "user_id": row["user_id"], "_evidence_id": row["_evidence_id"] + "," + new["_evidence_id"]}
            allowed = row["status"] == "unpaid" if name.startswith("pay_") else row["status"] != "cancelled"
            return {**row, "available": allowed}
        return super().resolve(action)
