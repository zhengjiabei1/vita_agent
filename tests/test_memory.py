import json
import math

import pytest

from vita_agent.memory import PreferenceMemory, extract_preferences
from vita_agent.models import Evidence


def evidence(id="e1", text="My budget is $100", timestamp=1.0, user="alice", source="user"):
    return Evidence(id, user, text, timestamp, source)


def budget(memory, e, value=100, domain="food"):
    return memory.remember(e, domain=domain, slot="total_price", value=value, strength="hard", operator="lte")


def soft(memory, e, value="sichuan", domain="food", slot="cuisine"):
    return memory.remember(e, domain=domain, slot=slot, value=value, strength="soft")


def test_supersession_stale_ingestion_history_and_idempotence():
    with PreferenceMemory() as memory:
        first = budget(memory, evidence())
        latest_e = evidence("e3", "Change my budget to $60", 3)
        latest = budget(memory, latest_e, 60)
        stale = budget(memory, evidence("e2", "Change my budget to $80", 2), 80)
        assert stale.status == "superseded"
        assert memory.active("alice")[0].value == 60
        history = memory.history("alice", "total_price")
        assert [p.status for p in history] == ["superseded", "superseded", "active"]
        assert history[1].supersedes == first.id
        assert history[2].supersedes == stale.id
        assert budget(memory, latest_e, 60).id == latest.id
        assert len(memory.history("alice", "total_price")) == 3


def test_equal_timestamp_conflict_rejected_without_side_effects():
    with PreferenceMemory() as memory:
        budget(memory, evidence())
        with pytest.raises(ValueError, match="equal timestamps"):
            budget(memory, evidence("e2", "My budget is $50"), 50)
        assert len(memory.history("alice", "total_price")) == 1
        assert memory.active("alice")[0].value == 100
        with pytest.raises(ValueError, match="different content"):
            budget(memory, evidence(text="My budget is $50", timestamp=2), 50)


def test_hard_constraints_injected_independently_of_soft_top_k():
    with PreferenceMemory() as memory:
        budget(memory, evidence())
        global_e = evidence("e2", "I am allergic to peanuts", 2)
        memory.remember(global_e, domain="global", slot="allergens", value=["peanut"], strength="hard", operator="excludes")
        budget(memory, evidence("e3", "My travel budget is 500", 3), 500, domain="travel")
        soft(memory, evidence("e4", "I prefer Sichuan", 4))
        rendered = json.loads(memory.render("alice", domain="food", top_k=0).split("\n", 1)[1])
        assert len(rendered["hard_constraints"]) == 2
        assert rendered["soft_preferences"] == []
        assert {p["domain"] for p in rendered["hard_constraints"]} == {"food", "global"}


def test_tiered_soft_retrieval_and_user_isolation():
    with PreferenceMemory() as memory:
        soft(memory, evidence("f", "I like Italian", 1), "italian", "food")
        soft(memory, evidence("g", "I prefer Sichuan", 2), "sichuan", "global")
        soft(memory, evidence("t", "I like Sichuan", 3), "sichuan", "travel")
        soft(memory, evidence("b", "I love Japanese", 4, user="bob"), "japanese", "food")
        preferences = memory.retrieve("alice", "sichuan", domain="food", top_k=10)
        assert [p.domain for p in preferences] == ["food", "global", "travel"]
        assert all(p.user_id == "alice" and p.strength == "soft" for p in preferences)
        assert len(memory.active("bob")) == 1
        memory.reset("alice")
        assert memory.active("alice") == []
        assert len(memory.active("bob")) == 1


@pytest.mark.parametrize("e", [
    evidence(source="tool"), evidence(text=""), evidence(user=""), evidence(timestamp=math.nan),
    evidence(timestamp=math.inf), evidence(timestamp=True), evidence(id=""),
])
def test_invalid_evidence_rejected(e):
    with PreferenceMemory() as memory:
        with pytest.raises(ValueError):
            budget(memory, e)
        with pytest.raises(ValueError):
            extract_preferences(e)


def test_quote_value_strength_and_operator_validation():
    with PreferenceMemory() as memory:
        for changes in ({"quote": "fabricated"}, {"quote": ""}, {"strength": "mandatory"},
                        {"operator": "less_than"}, {"value": math.nan}, {"value": True}):
            kwargs = dict(domain="food", slot="total_price", value=100, strength="hard", operator="lte")
            kwargs.update(changes)
            with pytest.raises(ValueError):
                memory.remember(evidence(), **kwargs)
        assert memory.active("alice") == []


def test_persistence_and_same_evidence_id_across_users(tmp_path):
    path = tmp_path / "nested" / "memory.sqlite3"
    with PreferenceMemory(path) as memory:
        a = budget(memory, evidence())
        budget(memory, evidence(user="bob", text="My budget is $30"), 30)
    with PreferenceMemory(path) as memory:
        assert memory.active("alice")[0].id == a.id
        assert memory.active("bob")[0].value == 30
        memory.reset()
        assert memory.active("alice") == []
        assert memory.active("bob") == []


def test_chinese_explicit_preferences_and_update():
    e = evidence(text="我的预算不超过100元，我对花生过敏，我喜欢川菜。现在预算改为80元。")
    extracted = {p["slot"]: p for p in extract_preferences(e, "food")}
    assert extracted["total_price"]["value"] == 80
    assert extracted["allergens"]["value"] == ["peanut"]
    assert extracted["cuisine"]["value"] == "sichuan"
    with PreferenceMemory() as memory:
        for item in extracted.values():
            assert item["quote"] in e.text
            memory.remember(e, **item)
        assert len(memory.active("alice")) == 3


def test_english_explicit_preferences_and_history_exclusion():
    e = evidence(text="My budget is $120. I am allergic to peanuts and shellfish. I prefer Italian cuisine.")
    extracted = {p["slot"]: p for p in extract_preferences(e, "food")}
    assert extracted["total_price"]["value"] == 120
    assert extracted["allergens"]["value"] == ["peanut", "shellfish"]
    assert extracted["cuisine"]["value"] == "italian"
    assert extract_preferences(evidence(text="上次点了川菜，花了100元。朋友对花生过敏。")) == []
    assert extract_preferences(evidence(text="我不喜欢川菜。I do not like Italian food.")) == []
    assert extract_preferences(evidence(text="之前预算100元。")) == []


def test_no_budget_from_nonbudget_numbers_and_no_allergy_from_third_party():
    assert extract_preferences(evidence(text="I ordered Italian food for 100 yesterday.")) == []
    assert extract_preferences(evidence(text="My friend is allergic to peanuts.")) == []


def test_unsupported_budget_units_and_per_person_scope_do_not_become_total_price():
    for text in ("预算100万元", "预算100元左右", "My budget is $100 per person"):
        assert extract_preferences(evidence(text=text)) == []
    e = evidence(text="My budget is $100.50. I prefer Italian.")
    assert {p["slot"]: p["value"] for p in extract_preferences(e)} == {
        "total_price": 100.5, "cuisine": "italian"}


@pytest.mark.parametrize("text", [
    "我对花生不过敏。", "我对花生不再过敏。", "我可能对花生过敏。",
    "我对花生可能过敏。", "如果我对花生过敏就避开它。", "我对花生过敏吗？",
    "I am not allergic to peanuts.", "It is not true that I am allergic to peanuts.",
    "I might be allergic to peanuts.", "I am allergic to peanuts maybe.",
    "If I am allergic to peanuts, avoid them.", "I am allergic to peanuts if that test is right.",
    "我喜欢川菜但现在不吃。", "我可能喜欢川菜。", "I prefer Italian only if my friend does.",
    "It is not true that I prefer Italian.",
])
def test_negation_uncertainty_and_counterfactual_preferences_are_not_extracted(text):
    assert extract_preferences(evidence(text=text), "food") == []


@pytest.mark.parametrize("text", [
    'My friend said: "I am allergic to peanuts. I prefer Italian."',
    "朋友说：“我对花生过敏。我喜欢川菜。”",
    "My friend said 'I am allergic to peanuts' yesterday.",
    '"I am allergic to peanuts" is an example, not my preference.',
    "医生写下「我对花生过敏。我喜欢川菜。」这句话。",
])
def test_quoted_third_party_or_example_preferences_are_not_extracted(text):
    assert extract_preferences(evidence(text=text), "food") == []


def test_unsupported_allergy_correction_keeps_prior_supported_constraint():
    with PreferenceMemory() as memory:
        original = evidence(text="我对花生过敏。")
        for item in extract_preferences(original, "food"):
            memory.remember(original, **item)
        correction = evidence("e2", "我对花生不再过敏。", timestamp=2)
        for item in extract_preferences(correction, "food"):
            memory.remember(correction, **item)
        active = memory.active("alice", domain="food", strength="hard")
        assert len(active) == 1
        assert active[0].value == ["peanut"]
        assert active[0].evidence_id == original.id
        assert len(memory.history("alice", "allergens")) == 1


def test_simple_unquoted_self_statements_remain_supported():
    e = evidence(text="I'm allergic to peanuts and shellfish. I prefer Italian cuisine.")
    extracted = {p["slot"]: p["value"] for p in extract_preferences(e, "food")}
    assert extracted == {"allergens": ["peanut", "shellfish"], "cuisine": "italian"}
