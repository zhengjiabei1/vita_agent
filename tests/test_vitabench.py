import os
from pathlib import Path
import tempfile
from unittest.mock import patch

import pytest

from vita_agent.models import Action
# Upstream loads model configuration even when only importing its type classes.
# This fixture contains no credentials and no usable remote endpoint.
with tempfile.TemporaryDirectory() as _config_dir:
    _config = Path(_config_dir) / "models.yaml"
    _config.write_text("default: {}\nmodels: []\n", encoding="utf-8")
    with patch.dict(os.environ, {"VITA_MODEL_CONFIG_PATH": str(_config)}):
        from vita_agent.vitabench import (
            ObservedFacts, StructuredPreferenceMemory, interaction_evidence,
        )


def interaction(text, date="2026-07-01", role="user", domain="delivery"):
    return {"date": date, "domain": domain, "dialogue": [{"role": role, "content": text}]}


def test_only_user_dialogue_is_evidence():
    record = {"date": "2026-07-01", "dialogue": [
        {"role": "assistant", "content": "My budget is 20"},
        {"role": "user", "content": "My budget is 40"},
        {"role": "tool", "content": "My budget is 10"}],
        "behavior": [{"content": "My budget is 999"}],
        "groundtruth": "My budget is 888", "evaluation_criteria": "My budget is 777"}
    rows = interaction_evidence(record, "u1")
    assert len(rows) == 1
    assert rows[0][0].text == "My budget is 40"
    assert rows[0][0].user_id == "u1"
    assert interaction_evidence({"type": "conversation", "timestamp": "2026-07-01",
                                 "content": "My budget is 20"}, "u1") == []


def test_interaction_object_and_timestamp_order():
    class Interaction:
        def model_dump(self):
            return {"type": "conversation", "timestamp": "2026-07-01 12:00:00",
                    "content": {"messages": [{"role": "user", "content": "My budget is 20"}]}}
    rows = interaction_evidence(Interaction(), "u1")
    assert len(rows) == 1
    assert rows == interaction_evidence(Interaction(), "u1")
    with pytest.raises(ValueError, match="timestamp"):
        interaction_evidence({"dialogue": [{"role": "user", "content": "hello"}]}, "u1")


def test_plugin_updates_supersede_without_assistant_or_behavior_leakage():
    memory = StructuredPreferenceMemory(user_id="u1", top_k=0)
    memory.update([interaction("我的预算是50元")])
    memory.update([interaction("我的预算是30元", "2026-07-02")])
    memory.update([interaction("我的预算是999元", "2026-07-03", role="assistant")])
    active = memory.store.active("u1", domain="delivery")
    assert len(active) == 1 and active[0].value == 30
    assert "30" in memory.read("hotel")  # Hard preferences survive top_k=0.
    assert len(memory.store.history("u1", "total_price")) == 2
    memory.reset()
    assert memory.store.active("u1") == []
    memory.close()


def test_plugin_instance_and_user_isolation():
    a, b = StructuredPreferenceMemory(user_id="same"), StructuredPreferenceMemory(user_id="same")
    a.update([interaction("我的预算是50元")])
    assert b.store.active("same") == []
    missing_a, missing_b = StructuredPreferenceMemory(), StructuredPreferenceMemory()
    assert missing_a.user_id != missing_b.user_id
    for memory in (a, b, missing_a, missing_b):
        memory.close()


def test_semantic_plugin_uses_quotes_and_deduplicates_paid_extraction():
    calls = []
    def generator(payload):
        calls.append(payload)
        row = payload["evidence"][0]
        return {"preferences": [{"evidence_id": row["evidence_id"], "domain": row["domain"],
            "slot": "bakery_type", "value": "handmade", "strength": "soft", "operator": "eq",
            "quote": "我喜欢手作面包", "certainty": "explicit"}],
            "cost": 0.01, "usage": {"prompt_tokens": 100, "completion_tokens": 50}}
    memory = StructuredPreferenceMemory(user_id="u1", extraction="llm", generator=generator)
    event = interaction("我喜欢手作面包")
    memory.update([event])
    memory.update([event])
    assert len(calls) == 1
    assert memory.store.active("u1")[0].value == "handmade"
    assert memory.extraction_costs == [0.01]
    assert "手作面包" in memory.read("面包")
    memory.close()


def test_history_preserves_unrecognized_user_preferences_without_model():
    memory = StructuredPreferenceMemory(user_id="u1", extraction="rules")
    memory.update([interaction("帮我找手作面包，最好配送快一点")])
    assert not memory.store.active("u1")
    assert "手作面包" in memory.read("面包")
    memory.close()


def test_corrupt_timestamp_and_unrecognized_format_reported():
    memory = StructuredPreferenceMemory()
    memory.update([interaction("我的预算是30元", "invalid"), {"groundtruth": "budget 80"}])
    assert [item["code"] for item in memory.diagnostics] == [
        "invalid_interaction_timestamp", "no_explicit_user_dialogue"]
    assert memory.store.active(memory.user_id) == []
    memory.close()


PRODUCT = "StoreProduct(store_name=shop, store_id=s1, product_name=rice, product_id=p1, attributes=regular, quantity=10, price=12.5, tags=['rice'])"
HOTEL = "Hotel Info:\nHotel(hotel_id=h1, hotel_name=Hotel, score=4.5, star_rating=3, location=x, tags=[], products=HotelProduct(room_type=double, date=2026-07-03, price=200.0, quantity=2, room_id=r1))"


def test_delivery_facts_are_grounded_in_read_reply_and_actual_quantities():
    facts = ObservedFacts()
    action = Action("create_delivery_order", {"product_ids": ["p1"], "product_cnts": [2], "store_id": "s1", "total_price": 1}, "key")
    assert facts.resolve(action) == {}
    facts.observe("assistant", PRODUCT, "fake")
    assert facts.resolve(action) == {}
    facts.observe("get_delivery_product_info", PRODUCT, "tool-result-1")
    assert facts.resolve(action) == {"total_price": 25.0, "available": True,
                                     "_evidence_id": "tool-result-1", "food_tags": ["rice"]}
    assert facts.resolve(Action(action.tool, {**action.arguments, "store_id": "wrong"}, "x")) == {}
    assert facts.resolve(Action(action.tool, {**action.arguments, "product_cnts": [True]}, "x")) == {}
    assert facts.resolve(Action(action.tool, {**action.arguments, "product_ids": ["p1", "p1"], "product_cnts": [1, 1]}, "x")) == {}


def test_delivery_minutes_are_parsed_and_enforced():
    from vita_agent.guards import ConstraintChecker
    from vita_agent.official_facts import get_official_specs
    from vita_agent.models import Preference
    product = PRODUCT.replace("attributes=regular", "attributes=清汤底, 配送时间约30分钟")
    facts = ObservedFacts()
    facts.observe("get_delivery_product_info", product, "tool-result-1")
    action = Action("create_delivery_order",
                    {"user_id": "u1", "product_ids": ["p1"], "product_cnts": [1],
                     "store_id": "s1", "address": "addr", "dispatch_time": "2026-07-01 13:00:00"},
                    "key")
    resolved = facts.resolve(action)
    assert resolved["delivery_minutes"] == 30
    pref = Preference("p1", "u1", "delivery", "delivery_minutes", 25, "hard", "lte",
                      "user-message-1", "25分钟以内", 1)
    violations = ConstraintChecker(get_official_specs(), unknown_policy="warn").check(action, [pref], resolved)
    assert "preference_conflict" in {v.code for v in violations}


def test_parser_rejects_ambiguous_or_nonfinite_fields():
    facts = ObservedFacts()
    for product in (PRODUCT.replace("rice, product_id", "rice, price=1, product_id"),
                    PRODUCT.replace("price=12.5", "price=nan"),
                    PRODUCT.replace("price=12.5", "price=1e999")):
        facts.observe("get_delivery_product_info", product, "tool1")
    assert facts.products == {}
    facts.observe("get_delivery_product_info", PRODUCT, "tool1", error=True)
    assert facts.products == {}


def test_hotel_facts_missing_allergens_stay_unknown_and_caches_clear():
    facts = ObservedFacts()
    facts.observe("get_ota_hotel_info", HOTEL, "hotel-detail")
    resolved = facts.resolve(Action("create_hotel_order", {"hotel_id": "h1", "room_id": "r1"}, "id"))
    assert resolved["total_price"] == 200
    assert resolved["available"]
    assert "allergens" not in resolved
    assert facts.resolve(Action("create_hotel_order", {"hotel_id": [], "room_id": {}}, "id")) == {}
    facts.clear()
    assert facts.rooms == {}


def test_hotel_brand_and_amenities_are_observed_for_guard_checks():
    facts = ObservedFacts()
    hotel = HOTEL.replace("hotel_name=Hotel,", "hotel_name=长春喜来登酒店,").replace(
        "tags=[]", "tags=['wifi', '电话叫醒']")
    facts.observe("get_ota_hotel_info", hotel, "hotel-detail")
    resolved = facts.resolve(Action("create_hotel_order", {"hotel_id": "h1", "room_id": "r1"}, "id"))
    assert resolved["hotel_brand"] == "sheraton"
    assert resolved["hotel_amenities"] == ["phone_wakeup"]


def test_hard_hotel_brand_preference_blocks_wrong_brand():
    from vita_agent.guards import ConstraintChecker
    from vita_agent.official_facts import get_official_specs
    from vita_agent.models import Preference
    facts = ObservedFacts()
    facts.observe("get_ota_hotel_info", HOTEL.replace("hotel_name=Hotel,", "hotel_name=长春JW万豪酒店,"),
                  "hotel-detail")
    resolved = facts.resolve(Action("create_hotel_order", {"hotel_id": "h1", "room_id": "r1"}, "id"))
    pref = Preference("p1", "u1", "ota", "hotel_brand", "sheraton", "hard", "eq",
                      "user-message-1", "只住喜来登", 1)
    violations = ConstraintChecker(get_official_specs(), unknown_policy="warn").check(
        Action("create_hotel_order", {"hotel_id": "h1", "room_id": "r1", "user_id": "u1"}, "id"),
        [pref], resolved)
    assert {v.code for v in violations} == {"preference_conflict"}


def test_guard_prompt_requires_explicit_constraint_evidence(monkeypatch):
    agent, memory, _ = _upstream_agent(monkeypatch, [])
    assert "explicitly state the chosen item/room" in agent.system_prompt
    memory.close()


def test_guard_prompt_surfaces_observed_candidates_meeting_hard_constraints(monkeypatch):
    pytest.importorskip("vita.environment.tool")
    from vita.environment.tool import as_tool
    from vita.environment.toolkit import is_tool, ToolType
    from vita_agent.models import Evidence

    @is_tool(ToolType.WRITE)
    def create_hotel_order(hotel_id: str, room_id: str, user_id: str) -> str:
        raise AssertionError("Guard must not execute tools")

    agent, memory, _ = _upstream_agent(monkeypatch, [])
    agent.update_tools([as_tool(create_hotel_order)])
    memory.store.remember(Evidence("e1", "u1", "房间要带浴缸", 1), domain="ota",
                          slot="hotel_feature.bathtub", value=True, strength="hard", operator="eq")
    search = ("Hotel(hotel_id=H1, hotel_name=目标酒店, score=4.4, star_rating=2, "
              "location=address='重庆市', longitude=106.4 latitude=29.56, "
              "tags=['经济型', '连锁酒店', 'wifi', '停车场', '浴缸'])")
    agent.observed_facts.observe("hotel_search_recommand", search, "tool-1")
    prompt = agent.system_prompt
    assert "Runtime candidates" in prompt and "目标酒店" in prompt
    memory.close()


def test_blocked_hotel_write_hint_lists_only_searched_hotels_meeting_hard_features(monkeypatch):
    from vita_agent.models import Preference
    agent, memory, _ = _upstream_agent(monkeypatch, [])
    good = ("Hotel(hotel_id=H1, hotel_name=目标酒店, score=4.4, star_rating=2, "
            "location=address='重庆市', longitude=106.4 latitude=29.56, "
            "tags=['经济型', '连锁酒店', 'wifi', '停车场', '浴缸'])")
    bad = ("Hotel(hotel_id=H2, hotel_name=无停车酒店, score=4.5, star_rating=2, "
           "location=address='重庆市', longitude=106.5 latitude=29.5, "
           "tags=['经济型', '连锁酒店', 'wifi', '浴缸'])")
    agent.observed_facts.observe("hotel_search_recommand", good + "\n" + bad, "tool-1")
    preferences = [Preference("p1", "u1", "ota", "hotel_feature.parking", True, "hard",
                              "eq", "e1", "带停车场", 1)]
    hint = agent._candidate_hint("create_hotel_order", preferences)
    assert "目标酒店" in hint and "无停车酒店" not in hint
    memory.close()


def test_blocked_delivery_write_hint_lists_only_items_meeting_delivery_limit(monkeypatch):
    from vita_agent.models import Preference
    agent, memory, _ = _upstream_agent(monkeypatch, [])
    content = (
        "Store(name=测试店, store_id=S1, score=4.5, location=addr, tags=['面食']), products="
        "StoreProduct(store_name=测试店, store_id=S1, product_name=快面, product_id=P1, "
        "attributes=清汤底, 配送时间约20分钟, quantity=10, price=16.0, tags=['面食', '清汤'])\n"
        "StoreProduct(store_name=测试店, store_id=S1, product_name=慢面, product_id=P2, "
        "attributes=清汤底, 配送时间约30分钟, quantity=10, price=18.0, tags=['面食', '清汤'])")
    agent.observed_facts.observe("get_delivery_store_info", content, "tool-2")
    preferences = [Preference("p1", "u1", "delivery", "delivery_minutes", 25.0, "hard",
                              "lte", "e1", "25分钟以内", 2)]
    hint = agent._candidate_hint("create_delivery_order", preferences)
    assert "快面" in hint and "慢面" not in hint
    memory.close()


def test_delivery_hint_never_claims_unknown_delivery_time_is_satisfied(monkeypatch):
    from vita_agent.models import Preference
    agent, memory, _ = _upstream_agent(monkeypatch, [])
    content = (
        "Store(name=测试店, store_id=S1, score=4.5, location=addr, tags=['面食']), products="
        "StoreProduct(store_name=测试店, store_id=S1, product_name=酸辣粉, product_id=P1, "
        "attributes=酸辣, quantity=10, price=16.0, tags=['粉类', '酸辣'])")
    agent.observed_facts.observe("get_delivery_store_info", content, "tool-1")
    preferences = [Preference("p1", "u1", "delivery", "delivery_minutes", 25.0, "hard",
                              "lte", "e1", "25分钟以内", 1)]
    hint = agent._candidate_hint("create_delivery_order", preferences)
    assert "酸辣粉" not in hint
    assert "evidence" in hint
    memory.close()


def test_new_unpaid_order_is_paid_by_the_guard_before_the_next_llm_turn(monkeypatch):
    pytest.importorskip("vita.data_model.message")
    from vita.data_model.message import ToolMessage
    agent, memory, calls = _upstream_agent(monkeypatch, [])
    observation = ToolMessage(
        role="tool", id="create1", name="create_delivery_order",
        content="Order(order_id:o1, order_type:delivery, user_id:u1, store_id:s1, total_price:25.0, status:unpaid, products:[])")
    result, _ = agent.generate_next_message(observation, agent.get_init_state())
    assert calls == []
    assert [call.name for call in result.tool_calls] == ["pay_delivery_order"]
    assert result.tool_calls[0].arguments == {"order_id": "o1"}
    memory.close()


def test_payment_authorization_executes_observed_unpaid_order_without_llm(monkeypatch):
    pytest.importorskip("vita.data_model.message")
    from vita.data_model.message import UserMessage
    agent, memory, calls = _upstream_agent(monkeypatch, [])
    agent.observed_facts.observe(
        "create_delivery_order",
        "Order(order_id:o1, order_type:delivery, user_id:u1, store_id:s1, total_price:25.0, status:unpaid, products:[])",
        "create1")
    result, _ = agent.generate_next_message(UserMessage(role="user", content="好的，支付吧"), agent.get_init_state())
    assert calls == []
    assert [call.name for call in result.tool_calls] == ["pay_delivery_order"]
    assert result.tool_calls[0].arguments == {"order_id": "o1"}
    memory.close()


def test_hard_hotel_feature_preference_blocks_missing_bathtub():
    from vita_agent.guards import ConstraintChecker
    from vita_agent.official_facts import get_official_specs
    from vita_agent.models import Preference
    facts = ObservedFacts()
    hotel = HOTEL.replace("hotel_name=Hotel,", "hotel_name=速8酒店,").replace(
        "tags=[]", "tags=['经济型', '连锁酒店', '停车场']")
    facts.observe("get_ota_hotel_info", hotel, "hotel-detail")
    resolved = facts.resolve(Action("create_hotel_order", {"hotel_id": "h1", "room_id": "r1"}, "id"))
    assert "bathtub" not in resolved["hotel_features"]
    pref = Preference("p1", "u1", "ota", "hotel_feature.bathtub", True, "hard", "eq",
                      "user-message-1", "房间要带浴缸", 1)
    violations = ConstraintChecker(get_official_specs(), unknown_policy="warn").check(
        Action("create_hotel_order", {"hotel_id": "h1", "room_id": "r1", "user_id": "u1"}, "id"),
        [pref], resolved)
    assert {v.code for v in violations} == {"preference_conflict"}


def test_room_bathtub_comes_from_room_type_not_the_hotel_tag():
    facts = ObservedFacts()
    hotel = HOTEL.replace("hotel_name=Hotel,", "hotel_name=速8酒店,").replace(
        "tags=[]", "tags=['经济型', '连锁酒店', '停车场', '浴缸']")
    facts.observe("get_ota_hotel_info", hotel, "hotel-detail")
    plain = facts.resolve(Action("create_hotel_order", {"hotel_id": "h1", "room_id": "r1"}, "id"))
    assert plain["hotel_feature.bathtub"] is False
    assert plain["hotel_feature.parking"] is True
    tub = ObservedFacts()
    tub.observe("get_ota_hotel_info", hotel.replace("room_type=double", "room_type=经济大床房(带浴缸)"), "room-detail")
    room = tub.resolve(Action("create_hotel_order", {"hotel_id": "h1", "room_id": "r1"}, "id"))
    assert room["hotel_feature.bathtub"] is True


def test_hotel_hint_does_not_resuggest_an_inspected_hotel_without_a_matching_room(monkeypatch):
    from vita_agent.models import Preference
    agent, memory, _ = _upstream_agent(monkeypatch, [])
    search = (
        "Hotel(hotel_id=H1, hotel_name=未检查酒店, score=4.4, star_rating=2, "
        "location=address='重庆市', longitude=106.4 latitude=29.56, "
        "tags=['经济型', '连锁酒店', 'wifi', '停车场', '浴缸'])\n"
        "Hotel(hotel_id=H2, hotel_name=已检查酒店, score=4.6, star_rating=2, "
        "location=address='重庆市', longitude=106.5 latitude=29.5, "
        "tags=['经济型', '连锁酒店', 'wifi', '停车场', '浴缸'])")
    agent.observed_facts.observe("hotel_search_recommand", search, "tool-1")
    inspected = (
        "Hotel Info:\nHotel(hotel_id=H2, hotel_name=已检查酒店, score=4.6, star_rating=2, "
        "location=x, tags=['经济型', '连锁酒店', 'wifi', '停车场', '浴缸'], "
        "products=HotelProduct(room_type=经济大床房, date=2026-07-03, price=138.0, quantity=2, room_id=r2))")
    agent.observed_facts.observe("get_ota_hotel_info", inspected, "tool-2")
    preferences = [Preference("p1", "u1", "ota", "hotel_feature.bathtub", True, "hard",
                              "eq", "e1", "房间要带浴缸", 1)]
    hint = agent._candidate_hint("create_hotel_order", preferences)
    assert "未检查酒店" in hint and "已检查酒店" not in hint
    memory.close()


def test_completion_claim_is_blocked_while_an_order_is_unpaid(monkeypatch):
    pytest.importorskip("vita.data_model.message")
    from vita.data_model.message import AssistantMessage, UserMessage
    drafts = [AssistantMessage(role="assistant", content="都安排好了，祝您愉快！"),
              AssistantMessage(role="assistant", content="还有订单未支付，需要我现在支付吗？")]
    agent, memory, _ = _upstream_agent(monkeypatch, drafts, max_regenerations=1)
    agent.observed_facts.observe(
        "create_delivery_order",
        "Order(order_id:o1, order_type:delivery, user_id:u1, store_id:s1, total_price:25.0, status:unpaid, products:[])",
        "create1")
    result, _ = agent.generate_next_message(UserMessage(role="user", content="好的，谢谢"), agent.get_init_state())
    assert result.content == "还有订单未支付，需要我现在支付吗？"
    assert any(v["code"] == "unpaid_order" for step in agent.guard_audit for v in step["violations"])
    memory.close()


def test_payment_only_has_facts_from_observed_correct_order_type():
    facts = ObservedFacts()
    facts.observe("create_delivery_order", "Order(order_id:o1, order_type:delivery, user_id:u1, store_id:s1, total_price:25.0, status:unpaid, products:[])", "create1")
    assert facts.resolve(Action("pay_delivery_order", {"order_id": "o1"}, "pay"))["total_price"] == 25
    assert facts.resolve(Action("pay_hotel_order", {"order_id": "o1"}, "pay")) == {}
    facts.observe("pay_delivery_order", "Payment successful", "pay")
    assert facts.orders == {}


def test_real_upstream_memory_factory_if_available():
    pytest.importorskip("vita.memory")
    from vita.memory import create_memory
    from vita.memory.base import BaseMemory
    memory = create_memory(memory_class="vita_agent.vitabench.StructuredPreferenceMemory", user_id="u1")
    assert isinstance(memory, BaseMemory)
    assert memory.get_tools() == {}
    memory.update([interaction("我的预算是30元")])
    assert memory.store.active("u1")[0].value == 30
    memory.close()


def _upstream_agent(monkeypatch, drafts, budget=30, max_regenerations=2, **options):
    pytest.importorskip("vita.agent.personalization_agent")
    from vita.environment.tool import as_tool
    from vita.environment.toolkit import is_tool, ToolType
    from vita.utils import llm_utils
    from vita_agent.vitabench import make_guarded_agent_class

    calls = []
    iterator = iter(drafts)

    def generate(**kwargs):
        calls.append(kwargs)
        return next(iterator)

    monkeypatch.setattr(llm_utils, "generate", generate)

    @is_tool(ToolType.READ)
    def get_delivery_product_info(product_id: str) -> str:
        raise AssertionError("Guard must not execute tools")

    @is_tool(ToolType.WRITE)
    def create_delivery_order(user_id: str, store_id: str, product_ids: list[str],
                              product_cnts: list[int], address: str, dispatch_time: str) -> str:
        raise AssertionError("Guard must not execute tools")

    @is_tool(ToolType.WRITE)
    def cancel_delivery_order(order_id: str) -> str:
        raise AssertionError("Unsupported tools must not execute")

    @is_tool(ToolType.WRITE)
    def pay_delivery_order(order_id: str) -> str:
        raise AssertionError("Guard must not execute tools")

    memory = StructuredPreferenceMemory(user_id="u1")
    memory.update([interaction(f"我的预算是{budget}元")])
    cls = make_guarded_agent_class()
    agent = cls(tools=[as_tool(get_delivery_product_info), as_tool(create_delivery_order),
                       as_tool(cancel_delivery_order), as_tool(pay_delivery_order)],
                domain_policy="Serve the user at {time}.", memory=memory, user_profile={"user_id": "u1"},
                llm="mock", time="2026-07-01 12:00:00", max_regenerations=max_regenerations, **options)
    return agent, memory, calls


def test_actual_guarded_subclass_regenerates_and_accumulates_cost(monkeypatch):
    pytest.importorskip("vita.data_model.message")
    from vita.data_model.message import AssistantMessage, ToolCall, ToolMessage, MultiToolMessage
    args = {"user_id": "u1", "store_id": "s1", "product_ids": ["p1"], "product_cnts": [3],
            "address": "test address", "dispatch_time": "2026-07-01 13:00:00"}
    drafts = [AssistantMessage(role="assistant", tool_calls=[ToolCall(id="bad", name="create_delivery_order", arguments=args)],
                               cost=0.1, usage={"prompt_tokens": 10, "completion_tokens": 2}),
              AssistantMessage(role="assistant", tool_calls=[ToolCall(id="good", name="create_delivery_order", arguments={**args, "product_cnts": [2]})],
                               cost=0.2, usage={"prompt_tokens": 15, "completion_tokens": 3})]
    agent, memory, calls = _upstream_agent(monkeypatch, drafts)
    observation = MultiToolMessage(role="tool", tool_messages=[ToolMessage(role="tool", id="read1", name="get_delivery_product_info", content=PRODUCT)])
    result, state = agent.generate_next_message(observation, agent.get_init_state())
    assert result.tool_calls[0].id == "good"
    assert result.cost == pytest.approx(0.3)
    assert result.usage == {"prompt_tokens": 25, "completion_tokens": 5}
    assert len(calls) == 2
    assert agent.guard_audit[0]["violations"][0]["code"] == "budget_exceeded"
    assert all(not getattr(message, "tool_calls", None) or message.tool_calls[0].id != "bad" for message in state.messages)
    memory.close()


def test_blocked_write_feedback_lists_observed_candidates(monkeypatch):
    pytest.importorskip("vita.data_model.message")
    from vita.data_model.message import AssistantMessage, MultiToolMessage, ToolCall, ToolMessage
    from vita_agent.models import Evidence
    store = (
        "Store(name=测试店, store_id=s1, score=4.5, location=addr, tags=['面食']), products="
        "StoreProduct(store_name=测试店, store_id=s1, product_name=慢面, product_id=p1, "
        "attributes=清汤底, 配送时间约30分钟, quantity=10, price=12.5, tags=['面食'])\n"
        "StoreProduct(store_name=测试店, store_id=s1, product_name=快面, product_id=p2, "
        "attributes=清汤底, 配送时间约20分钟, quantity=10, price=12.5, tags=['面食'])")
    args = {"user_id": "u1", "store_id": "s1", "product_ids": ["p1"], "product_cnts": [1],
            "address": "test address", "dispatch_time": "2026-07-01 13:00:00"}
    drafts = [AssistantMessage(role="assistant", tool_calls=[ToolCall(id="slow", name="create_delivery_order", arguments=args)]),
              AssistantMessage(role="assistant", content="换一家")]
    agent, memory, calls = _upstream_agent(monkeypatch, drafts)
    memory.store.remember(Evidence("e1", "u1", "配送25分钟以内", 1), domain="delivery",
                          slot="delivery_minutes", value=25.0, strength="hard", operator="lte")
    observation = MultiToolMessage(role="tool", tool_messages=[
        ToolMessage(role="tool", id="read-store", name="get_delivery_store_info", content=store)])
    result, _ = agent.generate_next_message(observation, agent.get_init_state())
    assert result.content == "换一家"
    assert any(v["code"] == "preference_conflict" for v in agent.guard_audit[0]["violations"])
    feedback = calls[1]["messages"][-1].content
    assert "快面" in feedback and "慢面" not in feedback
    memory.close()


def test_actual_guarded_subclass_blocks_unsupported_writes(monkeypatch):
    pytest.importorskip("vita.data_model.message")
    from vita.data_model.message import AssistantMessage, ToolCall, UserMessage
    draft = AssistantMessage(role="assistant", tool_calls=[ToolCall(id="mystery", name="mystery_write", arguments={"order_id": "o1"})], cost=0.1)
    agent, memory, calls = _upstream_agent(monkeypatch, [draft], max_regenerations=0)
    result, state = agent.generate_next_message(UserMessage(role="user", content="执行未知操作"), agent.get_init_state())
    assert not result.tool_calls
    assert "尚未执行" in result.content
    assert len(calls) == 1
    assert any(v["code"] == "unsupported_tool" for v in agent.guard_audit[0]["violations"])
    memory.close()


def test_later_soft_feature_cannot_erase_hard_feature():
    from vita_agent.models import Evidence
    memory = StructuredPreferenceMemory(user_id="u1")
    memory.store.remember(Evidence("hard-parking", "u1", "一定要带停车场", 1),
                          domain="global", slot="hotel_feature.parking", value=True,
                          strength="hard", operator="eq")
    memory.update([interaction("停车位有个保障", "2026-07-02")])
    hard = memory.store.active("u1", strength="hard")
    assert [(p.slot, p.value) for p in hard] == [("hotel_feature.parking", True)]
    soft = memory.store.active("u1", strength="soft")
    assert [p.slot for p in soft] == ["proposed.hotel_feature.parking"]
    memory.close()


def test_current_turn_llm_extraction_receives_agent_model(monkeypatch):
    pytest.importorskip("vita.data_model.message")
    from vita.data_model.message import AssistantMessage, UserMessage
    drafts = [AssistantMessage(role="assistant", content='{"preferences": []}'),
              AssistantMessage(role="assistant", content="ok")]
    agent, memory, calls = _upstream_agent(monkeypatch, drafts)
    memory.extraction = "llm"
    result, _ = agent.generate_next_message(UserMessage(role="user", content="我喜欢喜来登"), agent.get_init_state())
    assert result.content == "ok"
    assert calls[0]["model"] == "mock"
    assert "evidence" in str(calls[0]["messages"][-1].content)
    memory.close()


def test_current_user_turns_supersede_and_unknown_cost_stays_unknown(monkeypatch):
    pytest.importorskip("vita.data_model.message")
    from vita.data_model.message import AssistantMessage, UserMessage
    drafts = [AssistantMessage(role="assistant", content="已更新", cost=None),
              AssistantMessage(role="assistant", content="已更新", cost=0.1)]
    agent, memory, _ = _upstream_agent(monkeypatch, drafts)
    first, state = agent.generate_next_message(UserMessage(role="user", content="我的预算是40元"), agent.get_init_state())
    second, state = agent.generate_next_message(UserMessage(role="user", content="现在预算改为60元"), state)
    assert first.cost is None and first.usage is None
    assert second.cost == 0.1 and second.usage is None
    active = memory.store.active("u1", domain="delivery")
    assert len(active) == 1 and active[0].value == 60
    assert not memory.diagnostics
    memory.close()


def test_frozen_skill_prompt_and_explicit_guard_ablation(monkeypatch, tmp_path):
    pytest.importorskip("vita.data_model.message")
    from vita.data_model.message import AssistantMessage, ToolCall, UserMessage
    from vita_agent.skills import SkillBank
    bank = SkillBank()
    bank.distill({"trace_id": "private-trace", "task_id": "private-task", "split": "train",
                  "success": False, "domain": "delivery", "steps": [{"tool": "order", "violations": [
                      {"code": "budget_exceeded", "message": "private information"}]}]})
    snapshot = tmp_path / "frozen.json"
    bank.freeze(snapshot)
    bank.close()
    draft = AssistantMessage(role="assistant", tool_calls=[ToolCall(id="unchecked", name="cancel_delivery_order", arguments={"order_id": "o1"})])
    agent, memory, _ = _upstream_agent(monkeypatch, [draft], enable_guards=False, skill_bank=snapshot)
    agent.set_current_instruction("check budget before order")
    prompt = agent.system_prompt
    assert "full payable total" in prompt
    assert "private-trace" not in prompt and "private information" not in prompt
    result, _ = agent.generate_next_message(UserMessage(role="user", content="取消订单"), agent.get_init_state())
    assert result.tool_calls[0].id == "unchecked"
    assert agent.guard_audit == []
    memory.close()


def test_room_date_is_checked_against_simulated_time(monkeypatch):
    pytest.importorskip("vita.data_model.message")
    from vita.data_model.message import AssistantMessage, ToolCall, ToolMessage
    from vita.environment.tool import as_tool
    from vita.environment.toolkit import is_tool, ToolType

    @is_tool(ToolType.WRITE)
    def create_hotel_order(hotel_id: str, room_id: str, user_id: str) -> str:
        raise AssertionError("Must never execute")

    draft = AssistantMessage(role="assistant", tool_calls=[ToolCall(id="past", name="create_hotel_order", arguments={
        "hotel_id": "h1", "room_id": "r1", "user_id": "u1"})])
    agent, memory, _ = _upstream_agent(monkeypatch, [draft], max_regenerations=0)
    agent.update_tools([as_tool(create_hotel_order)])
    reply = ToolMessage(role="tool", id="old-hotel", name="get_ota_hotel_info", content=HOTEL.replace("2026-07-03", "2026-06-30"))
    result, _ = agent.generate_next_message(reply, agent.get_init_state())
    assert not result.tool_calls
    assert any(v["code"] == "invalid_dates" for v in agent.guard_audit[0]["violations"])
    memory.close()


def test_prompt_nudges_are_independent_switches(monkeypatch):
    monkeypatch.setenv("VITA_COMPLETION_EVIDENCE", "0")
    monkeypatch.setenv("VITA_CANDIDATE_HINTS", "0")
    agent, memory, _ = _upstream_agent(monkeypatch, [])
    prompt = agent.system_prompt
    assert "Before claiming completion" not in prompt
    assert "Runtime candidates" not in prompt
    memory.close()


def test_invalid_prompt_switch_is_rejected(monkeypatch):
    monkeypatch.setenv("VITA_CANDIDATE_HINTS", "yes")
    with pytest.raises(ValueError, match="VITA_CANDIDATE_HINTS"):
        _upstream_agent(monkeypatch, [])
