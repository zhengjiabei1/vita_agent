"""Opt-in, version-bound runtime repairs for two upstream taxi tool defects.

At VitaBench commit f60169e89f30499cb7883f3dad76bd03facc908d:
1. create_taxi_order decrements stock then calls DB.assign_order_id('taxi'),
   whose scenario table has no taxi entry and raises ValueError.
2. Order.__repr__/__str__ index id_str['taxi'], whose mapping also lacks taxi.

Apply before all experiment arms, including the unmodified baseline, and record
patch_info() with results. These repairs change no non-taxi tool semantics and do
not alter evaluation labels, database fields, rewards, or official source files.
The taxi rendering uses tagged JSON so every model field survives unambiguously.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import inspect
import json
from pathlib import Path
import threading
import uuid

UPSTREAM_COMMIT = "f60169e89f30499cb7883f3dad76bd03facc908d"
PATCH_ID = "vita-taxi-id-and-render-v1"
TAXI_ORDER_PREFIX = "VITA_TAXI_ORDER_V1 "
EXPECTED_SOURCES = {
    "vita.data_model.tasks": "71b7a86fbe33d1f2f380ab7046e0c4a06df9acfe13b1a39fabc3754984b07cda",
    "vita.environment.db": "b2c3138fdc725dbdd83f4c74ed4114a8cf12a8e053226ee53f11fec80c104f4a",
    "vita.domains.ota.tools": "a7b8f130c629df6ce5f43c45ddd8d656e7d9e3c0a682296ff5798a85bfb56a9e",
}
_ORIGINALS = None
_LOCK = threading.RLock()


def patch_info() -> dict:
    return {"patch_id": PATCH_ID, "upstream_commit": UPSTREAM_COMMIT,
            "source_sha256": dict(EXPECTED_SOURCES), "enabled": _ORIGINALS is not None}


def install_upstream_fixes() -> dict:
    """Validate pinned implementation hashes and install before starting workers."""
    global _ORIGINALS
    import importlib
    from vita.data_model.tasks import Order
    from vita.environment.db import DB
    with _LOCK:
        if _ORIGINALS is not None:
            return patch_info()
        for module_name, expected in EXPECTED_SOURCES.items():
            module = importlib.import_module(module_name)
            actual = hashlib.sha256(Path(inspect.getfile(module)).read_bytes()).hexdigest()
            if actual != expected:
                raise RuntimeError(f"Refusing {PATCH_ID}: {module_name} differs from pinned upstream {UPSTREAM_COMMIT}")
        original_repr, original_str, original_assign = Order.__repr__, Order.__str__, DB.assign_order_id

        def render(order, original):
            if order.order_type != "taxi":
                return original(order)
            return TAXI_ORDER_PREFIX + json.dumps(order.model_dump(mode="json"), ensure_ascii=False,
                sort_keys=True, separators=(",", ":"), allow_nan=False)

        def taxi_repr(order):
            return render(order, original_repr)

        def taxi_str(order):
            return render(order, original_str)

        def assign_order_id(db, scenario, user_id, **kwargs):
            if scenario != "taxi":
                return original_assign(db, scenario, user_id, **kwargs)
            if not isinstance(user_id, str) or not user_id:
                raise ValueError("Taxi order user_id must be nonempty")
            # Preserve the public OTA ID shape; never alter existing order IDs.
            while True:
                candidate = "OO" + uuid.uuid4().hex[:10]
                if candidate not in (db.orders or {}):
                    return candidate

        _ORIGINALS = (Order, DB, original_repr, original_str, original_assign)
        Order.__repr__, Order.__str__, DB.assign_order_id = taxi_repr, taxi_str, assign_order_id
        return patch_info()


def restore_upstream_fixes() -> None:
    """Restore original methods; intended for isolated tests, never mid-run."""
    global _ORIGINALS
    with _LOCK:
        if _ORIGINALS is not None:
            order_cls, db_cls, repr_fn, str_fn, assign_fn = _ORIGINALS
            order_cls.__repr__, order_cls.__str__, db_cls.assign_order_id = repr_fn, str_fn, assign_fn
            _ORIGINALS = None


@contextmanager
def patched_upstream():
    """Test scope that preserves a patch already installed by the experiment runner."""
    active = _ORIGINALS is not None
    info = install_upstream_fixes()
    try:
        yield info
    finally:
        if not active:
            restore_upstream_fixes()
