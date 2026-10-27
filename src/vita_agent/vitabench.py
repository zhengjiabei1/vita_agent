"""Optional integration with the official VitaBench 2.0 interfaces.

The memory class works with ``vita run --memory-class``. Guarded execution is an
opt-in runner because upstream constructs PersonalizationAgent directly. The
observed-fact bridge intentionally supports only delivery/hotel creation and
payment; unsupported writes fail closed. See docs/vitabench.md for boundaries.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import math
import os
import re
from uuid import uuid4

from .memory import (PreferenceMemory, extract_preferences, normalize_hotel_brand,
                     normalize_hotel_features, HOTEL_FEATURE_TOKENS)
from .models import Action, Evidence, Violation
from .interactions import interaction_evidence
from .semantic_memory import EvidenceExtractor, EvidenceHistory

UPSTREAM_URL = "https://github.com/meituan-longcat/VitaBench-2.0"
UPSTREAM_COMMIT = "f60169e89f30499cb7883f3dad76bd03facc908d"

try:
    from vita.memory.base import BaseMemory as _BaseMemory
    VITA_AVAILABLE = True
except ModuleNotFoundError as exc:
    if exc.name != "vita":
        raise  # An incomplete official install must not silently become the offline shim.
    VITA_AVAILABLE = False

    class _BaseMemory:
        """Offline shim; official integration requires installing VitaBench."""

        def __init__(self, language=None, top_k=5, **kwargs):
            self.language, self.top_k = language, top_k

        def get_tools(self):
            return {}

        @property
        def tools(self):
            return {}




class StructuredPreferenceMemory(_BaseMemory):
    """Official custom memory backend with isolated per-run SQLite by default.

    No ground-truth preferences, evaluator rubrics or hidden user intentions are
    accepted. Unsupported interaction formats are counted rather than inferred.
    Extraction is deterministic and deliberately limited to explicit slot forms.
    """

    def __init__(self, language=None, user_id=None, top_k=5, path=":memory:", extraction=None, generator=None, **kwargs):
        super().__init__(language=language, top_k=top_k, **kwargs)
        self.user_id = str(user_id) if user_id is not None else "run-" + uuid4().hex
        self.store = PreferenceMemory(path)
        self.domain: str | None = None
        self.diagnostics: list[dict] = []
        self.history = EvidenceHistory(":memory:" if str(path) == ":memory:" else str(path) + ".history")
        self.extraction = extraction or os.environ.get("VITA_MEMORY_EXTRACTION", "rules")
        if self.extraction not in ("rules", "llm"):
            raise ValueError("VITA_MEMORY_EXTRACTION must be rules or llm")
        self.generator = generator
        self._extracted: set[str] = set()
        self.extraction_costs: list[float | None] = []

    @staticmethod
    def _model_generator(llm, llm_args):
        if not llm:
            raise ValueError("Semantic extraction requires a configured model")
        from vita.utils.llm_utils import generate
        from vita.data_model.message import SystemMessage, UserMessage
        def invoke(payload):
            response = generate(model=llm, messages=[SystemMessage(role="system", content=payload["system"]),
                UserMessage(role="user", content=json.dumps(payload["evidence"], ensure_ascii=False))], **(llm_args or {}))
            try:
                data = json.loads(response.content or "")
            except (ValueError, TypeError):
                data = {}
            return {"preferences": data.get("preferences") if isinstance(data, dict) else None,
                    "cost": response.cost, "usage": response.usage}
        return invoke

    def _remember_evidence(self, evidence: Evidence, item: dict) -> None:
        """A later soft opinion cannot erase an existing hard constraint."""
        if item.get("strength") == "soft" and any(
                preference.slot == item.get("slot")
                and preference.domain in (item.get("domain"), "global")
                for preference in self.store.active(self.user_id, strength="hard")):
            item["slot"] = "proposed." + str(item["slot"])
        self.store.remember(evidence, **item)

    def read(self, query=None) -> str:
        structured = self.store.render(self.user_id, query or "", domain=self.domain, top_k=self.top_k)
        history = self.history.render(self.user_id, query or "", domain=self.domain, top_k=self.top_k,
            inactive_evidence_ids=self.store.inactive_evidence_ids(self.user_id))
        return structured + "\n\n" + history

    def update(self, new_interactions, llm=None, llm_args=None, **kwargs) -> str:
        pending = {}
        for interaction in new_interactions:
            try:
                evidence_rows = interaction_evidence(interaction, self.user_id)
            except (ValueError, TypeError, OverflowError):
                self.diagnostics.append({"code": "invalid_interaction_timestamp"})
                continue
            if not evidence_rows:
                self.diagnostics.append({"code": "no_explicit_user_dialogue"})
            for evidence, domain in evidence_rows:
                self.history.add(evidence, domain)
                if evidence.id not in self._extracted:
                    pending[evidence.id] = (evidence, domain)
                for item in extract_preferences(evidence, domain=domain):
                    try:
                        self._remember_evidence(evidence, dict(item))
                    except ValueError as exc:
                        self.diagnostics.append({"code": "preference_rejected", "reason": str(exc)})
        if self.extraction == "llm" and pending:
            extractor = EvidenceExtractor(self.generator or self._model_generator(llm, llm_args))
            rows = list(pending.values())
            # Explicit bounded batches; no unmetered retries for invalid model JSON.
            for offset in range(0, len(rows), 12):
                batch = rows[offset:offset + 12]
                result = extractor.extract(batch)
                self.extraction_costs.append(result.cost)
                self.diagnostics.extend(result.diagnostics)
                evidence_by_id = {e.id: e for e, _ in batch}
                for candidate in result.candidates:
                    item = dict(candidate)
                    evidence = evidence_by_id[item.pop("evidence_id")]
                    try:
                        self._remember_evidence(evidence, item)
                    except ValueError:
                        self.diagnostics.append({"code": "semantic_preference_rejected"})
                self._extracted.update(e.id for e, _ in batch)
        return self.read()

    def reset(self):
        self.store.reset(self.user_id)
        self.diagnostics.clear()
        self.history.reset(self.user_id)
        self._extracted.clear()
        self.extraction_costs.clear()

    def close(self):
        self.store.close()
        self.history.close()


_ID = r"[A-Za-z0-9_-]+"
_NUMBER = r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"


class ObservedFacts:
    """Strict bridge from observed official read-tool replies, never model claims.

    Upstream serializes objects using custom repr strings. Only numeric product
    facts with unambiguous identifiers are parsed; missing allergens stay unknown.
    Caches must be cleared between subtasks and after mutations. No hidden DB or
    evaluator fields are read. JSON tool replies are deliberately not invented.
    """

    def __init__(self):
        self.products: dict[str, dict] = {}
        self.stores: dict[str, dict] = {}
        self.hotels: dict[str, dict] = {}
        self.rooms: dict[tuple[str, str], dict] = {}
        self.orders: dict[str, dict] = {}

    def clear(self):
        self.products.clear()
        self.stores.clear()
        self.hotels.clear()
        self.rooms.clear()
        self.orders.clear()

    def _delivery_products(self, content: str, evidence_id: str):
        pattern = rf"StoreProduct\(store_name=[^,]*, store_id=({_ID}), product_name=([^,]*), product_id=({_ID}), attributes=(.*?), quantity=({_NUMBER}), price=({_NUMBER}), tags=\[([^\]]*)\]\)"
        for match in re.finditer(pattern, content):
            store_id, name, product_id, attributes, quantity, price, tag_text = match.groups()
            if not (math.isfinite(float(quantity)) and math.isfinite(float(price))):
                continue
            minutes = re.search(r"配送时间约(\d+)分钟", attributes)
            self.products[product_id] = {
                "store_id": store_id, "quantity": float(quantity), "price": float(price),
                "product_name": name,
                "delivery_minutes": int(minutes[1]) if minutes else None,
                "product_tags": re.findall(r"'([^']*)'", tag_text),
                "_evidence_id": evidence_id,
            }

    def observe(self, name: str, content: str, evidence_id: str, error: bool = False):
        if error or not isinstance(content, str):
            return
        if name == "get_delivery_store_info":
            header = re.match(rf"Store\(name=([^,]+), store_id=({_ID}), score=({_NUMBER}),", content)
            if header:
                tag_block = re.search(r"tags=\[([^\]]*)\]", content.split(", products=")[0])
                tags = re.findall(r"'([^']*)'", tag_block[1]) if tag_block else []
                minutes = re.search(r"配送约(\d+)分钟", " ".join(tags))
                self.stores[header[2]] = {
                    "store_name": header[1], "store_score": float(header[3]),
                    "store_tags": tags, "delivery_minutes": int(minutes[1]) if minutes else None,
                    "_evidence_id": evidence_id,
                }
                self._delivery_products(content, evidence_id)
        elif name == "get_delivery_product_info":
            self._delivery_products(content, evidence_id)
        elif name in ("hotel_search_recommand", "get_ota_hotel_info"):
            # Keep every searched hotel's tags so a blocked write can point the
            # agent at an observed hotel that satisfies the hard constraints.
            for match in re.finditer(rf"Hotel\(hotel_id=({_ID}), hotel_name=([^,\n]+),.*?tags=\[([^\]]*)\]\)", content):
                hotel_id, hotel_name, tag_text = match.groups()
                self.hotels[hotel_id] = {"hotel_name": hotel_name,
                    "tags": re.findall(r"'([^']*)'", tag_text), "_evidence_id": evidence_id}
            if name == "hotel_search_recommand":
                return
            header = re.match(rf"Hotel Info:\nHotel\(hotel_id=({_ID}), hotel_name=([^,\n]+),", content)
            if not header or content.count("hotel_id=") != 1:
                return
            tag_block = re.search(r"tags=\[([^\]]*)\]", content.split(", products=")[0])
            hotel_tags = re.findall(r"'([^']*)'", tag_block[1]) if tag_block else []
            amenities = ["phone_wakeup"] if any("电话叫醒" in tag for tag in hotel_tags) else []
            features = normalize_hotel_features(hotel_tags)
            room_pattern = rf"HotelProduct\(room_type=([^,\n]+), date=(\d{{4}}-\d{{2}}-\d{{2}}), price=({_NUMBER}), quantity=({_NUMBER}), room_id=({_ID})\)"
            for match in re.finditer(room_pattern, content):
                room_type, date, price, quantity, room_id = match.groups()
                if math.isfinite(float(price)) and math.isfinite(float(quantity)):
                    room_features = sorted(set(features) | set(normalize_hotel_features([room_type])))
                    facts = {
                        "total_price": float(price), "available": float(quantity) > 0,
                        "date": date, "room_type": room_type,
                        "hotel_name": header[2], "hotel_tags": hotel_tags,
                        "hotel_brand": normalize_hotel_brand(header[2]),
                        "hotel_features": room_features,
                        "hotel_amenities": amenities,
                        "_evidence_id": evidence_id,
                    }
                    for feature in HOTEL_FEATURE_TOKENS:
                        facts[f"hotel_feature.{feature}"] = feature in room_features
                    self.rooms[(header[1], room_id)] = facts
        elif name in ("create_delivery_order", "create_hotel_order"):
            expected_type = "delivery" if name == "create_delivery_order" else "hotel"
            header = re.match(rf"Order\(order_id:({_ID}), order_type:({expected_type}), user_id:({_ID}),", content)
            prices = re.findall(rf"(?:^|, )total_price:({_NUMBER}),", content)
            status = re.search(r", status:([A-Za-z_]+),", content)
            if header and len(prices) == 1 and math.isfinite(float(prices[0])):
                self.orders[header[1]] = {"total_price": float(prices[0]), "available": True,
                    "user_id": header[3], "order_type": header[2],
                    "status": status[1] if status else "unknown", "_evidence_id": evidence_id}
            # Inventory observations become stale after any attempted creation.
            self.products.clear()
            self.rooms.clear()
        elif name.startswith("pay_"):
            self.orders.clear()

    def resolve(self, action: Action) -> dict:
        args = action.arguments
        if action.tool == "create_delivery_order":
            ids, counts = args.get("product_ids"), args.get("product_cnts")
            if not isinstance(ids, list) or not ids or not isinstance(counts, list) or len(ids) != len(counts):
                return {}
            if any(not isinstance(item, str) for item in ids) or len(set(ids)) != len(ids):
                return {}
            if any(not isinstance(count, int) or isinstance(count, bool) or count <= 0 for count in counts):
                return {}
            products = [self.products.get(item) for item in ids]
            if any(not product or product["store_id"] != args.get("store_id") for product in products):
                return {}
            facts = {"total_price": sum(p["price"] * n for p, n in zip(products, counts)),
                     "available": all(p["quantity"] >= n for p, n in zip(products, counts)),
                     "_evidence_id": ",".join(p["_evidence_id"] for p in products)}
            store = self.stores.get(args.get("store_id"), {})
            for key in ("store_name", "store_score", "store_tags", "delivery_minutes"):
                if key in store and store[key] is not None:
                    facts[key] = store[key]
            product_minutes = [p.get("delivery_minutes") for p in products if p.get("delivery_minutes") is not None]
            if product_minutes:
                facts["delivery_minutes"] = max(product_minutes)
            facts["food_tags"] = sorted({tag for p in products for tag in p.get("product_tags", [])})
            return facts
        if action.tool == "create_hotel_order":
            if not all(isinstance(args.get(k), str) for k in ("hotel_id", "room_id")):
                return {}
            return dict(self.rooms.get((args.get("hotel_id"), args.get("room_id")), {}))
        if action.tool in ("pay_delivery_order", "pay_hotel_order"):
            if not isinstance(args.get("order_id"), str):
                return {}
            facts = self.orders.get(args.get("order_id"), {})
            expected = "delivery" if action.tool == "pay_delivery_order" else "hotel"
            return dict(facts) if facts.get("order_type") == expected else {}
        return {}


def make_guarded_agent_class():
    """Build the optional actual upstream subclass only when VitaBench is installed."""
    from vita.agent.personalization_agent import PersonalizationAgent
    from vita.data_model.message import AssistantMessage, MultiToolMessage, SystemMessage, ToolCall
    from vita.utils.llm_utils import generate
    from .guards import ConstraintChecker, ToolSpec
    from .official_facts import OfficialObservedFacts, get_official_specs, ignores_purchase_preferences

    class GuardedPersonalizationAgent(PersonalizationAgent):
        def __init__(self, *args, max_regenerations=2, enable_guards=None, skill_bank=None, **kwargs):
            if not isinstance(max_regenerations, int) or not 0 <= max_regenerations <= 10:
                raise ValueError("max_regenerations must be an integer in [0, 10]")
            super().__init__(*args, **kwargs)
            if enable_guards is None:
                setting = os.environ.get("VITA_ENABLE_GUARDS", "1")
                if setting not in ("0", "1"):
                    raise ValueError("VITA_ENABLE_GUARDS must be 0 or 1")
                enable_guards = setting == "1"
            if not isinstance(enable_guards, bool):
                raise ValueError("enable_guards must be boolean")
            self.enable_guards = enable_guards
            from .skills import FrozenSkillBank
            skill_bank = skill_bank or os.environ.get("VITA_SKILL_BANK")
            self._frozen_skills = (skill_bank if isinstance(skill_bank, FrozenSkillBank)
                                   else FrozenSkillBank.load(skill_bank) if skill_bank else None)
            self.max_regenerations = max_regenerations
            self.observed_facts = OfficialObservedFacts()
            self.guard_audit: list[dict] = []
            self._submitted_writes: set[str] = set()
            self._user_turn = 0
            self._current_constraints = PreferenceMemory()
            self._configure_checker()

        def _unpaid_order_items(self):
            return [(order_id, order) for order_id, order in self.observed_facts.orders.items()
                    if order.get("status") not in ("paid", "cancelled", "refunded")]

        def _unpaid_orders(self):
            return [order for _, order in self._unpaid_order_items()]

        def _candidate_hint(self, tool, preferences):
            """List already observed candidates that pass the hard preference checks.

            Built only from read-tool replies, so it cannot introduce values the
            agent has not seen. The hint is advisory text, never a bypass: the
            next draft still has to pass the same checks.
            """
            if not preferences or tool not in ("create_hotel_order", "create_delivery_order"):
                return ""
            acceptable: list[str] = []

            def conflicts(facts, arguments):
                checked = self.checker.check(Action(tool, arguments, "candidate"), preferences, facts)
                return [v for v in checked if v.code == "preference_conflict"]

            if tool == "create_hotel_order":
                for (hotel_id, room_id), facts in self.observed_facts.rooms.items():
                    if not facts.get("available"):
                        continue
                    if not conflicts(facts, {"hotel_id": hotel_id, "room_id": room_id}):
                        acceptable.append(f"{facts.get('hotel_name')} / {facts.get('room_type')} / "
                                          f"{facts.get('date')} / {facts.get('total_price')}元")
                if acceptable:
                    return ("Observed rooms that satisfy every known hard constraint: "
                            + "; ".join(acceptable[:5]) + ". Book one of these observed rooms.")
                for hotel_id, hotel in self.observed_facts.hotels.items():
                    tags = hotel.get("tags") or []
                    features = normalize_hotel_features(tags)
                    facts = {"hotel_features": features, "hotel_tags": tags,
                             "hotel_brand": normalize_hotel_brand(hotel.get("hotel_name", "")),
                             "hotel_amenities": [], "_evidence_id": hotel.get("_evidence_id", "")}
                    for feature in HOTEL_FEATURE_TOKENS:
                        facts[f"hotel_feature.{feature}"] = feature in features
                    if not conflicts(facts, {"hotel_id": hotel_id, "room_id": "unobserved"}):
                        acceptable.append(f"{hotel.get('hotel_name')} ({hotel_id})")
                if acceptable:
                    return ("No inspected room passed yet, but these already searched hotels carry the required "
                            "features: " + "; ".join(acceptable[:5])
                            + ". Inspect one with get_ota_hotel_info and book an observed room.")
                return "No observed hotel candidate satisfies all hard constraints; search more or ask which constraint to relax."

            for product_id, product in self.observed_facts.products.items():
                if product.get("quantity", 0) < 1:
                    continue
                store = self.observed_facts.stores.get(product.get("store_id"), {})
                facts = {"total_price": product.get("price"), "available": True,
                         "_evidence_id": product.get("_evidence_id", ""),
                         "food_tags": sorted(product.get("product_tags", []))}
                for key in ("store_name", "store_score", "store_tags", "delivery_minutes"):
                    if store.get(key) is not None:
                        facts[key] = store[key]
                if product.get("delivery_minutes") is not None:
                    facts["delivery_minutes"] = product["delivery_minutes"]
                arguments = {"product_ids": [product_id], "product_cnts": [1],
                             "store_id": product.get("store_id")}
                if not conflicts(facts, arguments):
                    minutes = facts.get("delivery_minutes")
                    acceptable.append(f"{product.get('product_name')} @ {facts.get('store_name')}"
                                      f"{'' if minutes is None else f' / {minutes}分钟'} / {product.get('price')}元")
            if acceptable:
                return "Observed items that satisfy every known hard constraint: " + "; ".join(acceptable[:5]) + "."
            return "No observed delivery item satisfies all hard constraints; inspect other stores or ask which constraint to relax."

        @staticmethod
        def _payment_authorized(text):
            return isinstance(text, str) and bool(re.search(
                r"支付吧|现在支付|确认支付|去支付|帮我支付|付款|支付成功", text))

        @staticmethod
        def _claims_completion(text):
            if not isinstance(text, str):
                return False
            return bool(re.search(
                r"已完成|都订好|都安排|搞定|支付成功|已支付|没有其他|祝您|祝旅途|安排妥当|已确认|###STOP###",
                text))

        @property
        def system_prompt(self):
            prompt = super().system_prompt
            bank = getattr(self, "_frozen_skills", None)
            if bank is not None:
                domain = getattr(self, "_domain", None) or "general"
                skills = bank.retrieve(self._current_instruction or "", domain=domain, limit=5)
                if skills:
                    prompt += ("\n\nReusable procedural guidance, subordinate to current evidence and hard constraints:\n"
                               + "\n".join("- " + skill.lesson for skill in skills))
            if self._unpaid_orders():
                prompt += ("\n\nRuntime state: there are unpaid orders in this subtask. "
                           "Do not claim the task is complete; pay them or ask the user before finishing.")
            if self.enable_guards:
                prompt += ("\n\nBefore claiming completion, explicitly state the chosen item/room, price, "
                           "address or date, delivery/check-in time, and how each known hard constraint is satisfied. "
                           "Cite the observed values; do not guess missing fields.")
            return prompt

        def _configure_checker(self):
            specs = get_official_specs()
            for tool in self.tools:
                kind = getattr(getattr(tool, "_func", None), "__tool_type__", None)
                kind = getattr(kind, "value", kind)
                if kind in ("read", "generic", "think"):
                    required = tuple(tool.openai_schema.get("function", {}).get("parameters", {}).get("required", ()))
                    specs[tool.name] = ToolSpec(required, read_only=True)
            self._specs = specs
            # Official observations are incomplete by design. Parsing failures
            # must fall back to baseline behavior instead of blocking a valid
            # write forever; explicit parsed violations still fail closed.
            unknown_policy = os.environ.get("VITA_CGL_UNKNOWN_POLICY", "warn")
            self.checker = ConstraintChecker(specs, unknown_policy=unknown_policy)
            domains = {spec.domain for name, spec in specs.items()
                       if name in {t.name for t in self.tools} and spec.domain}
            self._domain = next(iter(domains)) if len(domains) == 1 else None
            if isinstance(self.memory, StructuredPreferenceMemory):
                self.memory.domain = self._domain

        def update_tools(self, tools):
            super().update_tools(tools)
            self.observed_facts.clear()
            self._submitted_writes.clear()
            self._user_turn = 0
            self._current_constraints.reset()
            self._configure_checker()
            domains = {spec.domain for name, spec in self._specs.items()
                       if name in {t.name for t in tools} and spec.domain}
            if isinstance(self.memory, StructuredPreferenceMemory):
                self.memory.domain = next(iter(domains)) if len(domains) == 1 else None

        def reset_state(self, message_history=None):
            self.observed_facts.clear()
            self._submitted_writes.clear()
            self._user_turn = 0
            self._current_constraints.reset()
            return super().reset_state(message_history)

        def generate_next_message(self, message, state):
            incoming = message.tool_messages if isinstance(message, MultiToolMessage) else [message]
            for item in incoming:
                if item.role == "tool":
                    self.observed_facts.observe(item.name, item.content, item.id, item.error)
                elif item.role == "user" and isinstance(self.memory, StructuredPreferenceMemory):
                    self._user_turn += 1
                    turn_time = (datetime.fromisoformat(self.time[:19])
                                 + timedelta(microseconds=self._user_turn)).isoformat()
                    self.memory.update([{"type": "conversation", "timestamp": turn_time,
                        "domain": self.memory.domain or "global",
                        "content": {"role": "user", "content": item.content}}],
                        llm=self.llm, llm_args=self.llm_args)
                elif item.role == "user" and self.enable_guards:
                    self._user_turn += 1
                    evidence = Evidence(f"turn-{self._user_turn}", "current-user", item.content or "",
                        float(self._user_turn))
                    if evidence.text:
                        for candidate in extract_preferences(evidence, self._domain or "global"):
                            self._current_constraints.remember(evidence, **candidate)
            state.messages.extend(incoming)
            # Refresh memory every turn, including preferences stated in this turn.
            state.system_messages = [SystemMessage(role="system", content=self.system_prompt)]
            created = any(item.role == "tool" and not item.error
                          and isinstance(item.name, str) and item.name.startswith("create_")
                          for item in incoming)
            if self.enable_guards and (created or any(
                    self._payment_authorized(item.content) for item in incoming if item.role == "user")):
                available = {tool.name for tool in self.tools}
                calls = []
                for order_id, order in self._unpaid_order_items():
                    name = f"pay_{order.get('order_type')}_order"
                    if name in available:
                        calls.append(ToolCall(id="auto-pay-" + uuid4().hex, name=name,
                                              arguments={"order_id": order_id}))
                if calls:
                    draft = AssistantMessage(role="assistant", tool_calls=calls, cost=0.0,
                                             usage={"prompt_tokens": 0, "completion_tokens": 0})
                    state.messages.append(draft)
                    self._submitted_writes.update(json.dumps([call.name, call.arguments], sort_keys=True)
                                                  for call in calls)
                    return draft, state
            feedback = []
            cost = 0.0
            usage = {"prompt_tokens": 0, "completion_tokens": 0}
            for attempt in range(self.max_regenerations + 1):
                draft = generate(model=self.llm, tools=self.tools,
                    messages=state.system_messages + state.messages + feedback,
                    enable_think=self.enable_think, **self.llm_args)
                if draft is None:
                    raise RuntimeError("LLM returned no message")
                cost = cost + draft.cost if cost is not None and draft.cost is not None else None
                if usage is not None:
                    if draft.usage is None or any(key not in draft.usage for key in usage):
                        usage = None
                    else:
                        for key in usage:
                            usage[key] += draft.usage[key]
                if not self.enable_guards:
                    draft.cost, draft.usage = cost, usage
                    state.messages.append(draft)
                    return draft, state
                violations = []
                hints: list[str] = []
                writes = 0
                write_signatures = []
                for call in draft.tool_calls or []:
                    action = Action(call.name, call.arguments, call.id or "proposal")
                    spec = self._specs.get(call.name)
                    writes += int(spec is None or not spec.read_only)
                    if spec is None or not spec.read_only:
                        signature = json.dumps([call.name, call.arguments], sort_keys=True, ensure_ascii=False)
                        write_signatures.append(signature)
                        if signature in self._submitted_writes:
                            violations.append(Violation("duplicate_write", "This write was already submitted; inspect its result instead of retrying"))
                    facts = self.observed_facts.resolve(action)
                    preferences = (self.memory.store.active(self.memory.user_id,
                        domain=spec.domain if spec else None, strength="hard")
                        if isinstance(self.memory, StructuredPreferenceMemory) else
                        self._current_constraints.active("current-user", domain=spec.domain if spec else None, strength="hard"))
                    checked = self.checker.check(action, [] if ignores_purchase_preferences(call.name) else preferences, facts)
                    violations.extend(checked)
                    if any(v.code == "preference_conflict" for v in checked):
                        hint = self._candidate_hint(call.name, preferences)
                        if hint:
                            hints.append(hint)
                    if call.name not in {tool.name for tool in self.tools}:
                        violations.append(Violation("unsupported_tool", "Tool is not available in this subtask"))
                    expected_user = self.user_profile.get("user_id")
                    if spec and not spec.read_only and not expected_user:
                        violations.append(Violation("missing_evidence", "Current account identity is unavailable"))
                    if expected_user and call.arguments.get("user_id", expected_user) != expected_user:
                        violations.append(Violation("user_mismatch", "Action user_id differs from the current account"))
                    if expected_user and facts.get("user_id", expected_user) != expected_user:
                        violations.append(Violation("user_mismatch", "Observed order belongs to another account"))
                    if spec and not spec.read_only and not ignores_purchase_preferences(call.name):
                        for field in spec.date_fields:
                            if field not in call.arguments:
                                continue
                            try:
                                proposed = datetime.fromisoformat(call.arguments[field])
                                now = datetime.fromisoformat(self.time[:19])
                                too_old = proposed.date() < now.date() if len(call.arguments[field]) == 10 else proposed < now
                                if too_old:
                                    violations.append(Violation("invalid_dates", f"{field} is earlier than simulation time"))
                            except (ValueError, TypeError):
                                pass  # Schema checker reports malformed or incomparable dates.
                        if facts.get("date"):
                            try:
                                if datetime.fromisoformat(facts["date"]).date() < datetime.fromisoformat(self.time[:19]).date():
                                    violations.append(Violation("invalid_dates", "Observed product date is earlier than simulation time"))
                            except (ValueError, TypeError):
                                violations.append(Violation("invalid_dates", "Observed product date is invalid"))
                if writes and len(draft.tool_calls or []) > 1:
                    violations.append(Violation("multiple_mutations", "Emit one write call per turn after observing its inputs"))
                if not draft.tool_calls and self._unpaid_orders() and self._claims_completion(draft.content):
                    violations.append(Violation(
                        "unpaid_order", "The draft claims completion while an observed order is still unpaid"))
                if not violations:
                    self._submitted_writes.update(write_signatures)
                    draft.cost, draft.usage = cost, usage
                    state.messages.append(draft)
                    return draft, state
                detail = [v.to_dict() for v in violations]
                self.guard_audit.append({"attempt": attempt + 1, "violations": detail})
                feedback = [SystemMessage(role="system", content=
                    "Pre-execution checks rejected the draft. No tool was executed. Regenerate or ask for missing information. "
                    + json.dumps(detail, ensure_ascii=False)
                    + "".join("\n" + hint for hint in dict.fromkeys(hints)))]
            blocked = AssistantMessage(role="assistant", content=
                "当前操作缺少可验证的信息或违反已知约束，尚未执行。请补充信息或调整方案。",
                cost=cost, usage=usage)
            state.messages.append(blocked)
            return blocked, state

    return GuardedPersonalizationAgent
