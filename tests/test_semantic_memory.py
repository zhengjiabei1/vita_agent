import json

import pytest

from vita_agent.memory import PreferenceMemory
from vita_agent.models import Evidence
from vita_agent.semantic_memory import EvidenceExtractor, EvidenceHistory


def row(**updates):
    value = {"evidence_id": "e1", "quote": "My budget is 30", "domain": "delivery",
             "slot": "total_price", "value": 30, "strength": "hard", "operator": "lte",
             "certainty": "explicit"}
    value.update(updates)
    return value


def extract(candidate, evidence=None, **metadata):
    evidence = evidence or Evidence("e1", "u1", "My budget is 30", 1)
    return EvidenceExtractor(lambda payload: {"preferences": [candidate], **metadata}).extract([(evidence, "delivery")])


def test_confirmed_budget_and_actual_call_accounting():
    result = extract(row(), cost=0.012, usage={"prompt_tokens": 200, "completion_tokens": 30})
    assert result.candidates[0]["strength"] == "hard"
    assert result.candidates[0]["slot"] == "total_price"
    assert result.cost == 0.012 and result.usage["prompt_tokens"] == 200
    assert result.diagnostics == []


def test_unverified_hard_cannot_supersede_real_constraint():
    old = Evidence("old", "u1", "My budget is 30", 1)
    newer = Evidence("e1", "u1", "This restaurant looks expensive to me", 2)
    result = extract(row(quote=newer.text, value=300), newer)
    candidate = result.candidates[0]
    assert candidate["strength"] == "soft" and candidate["slot"] == "proposed.total_price"
    with PreferenceMemory() as store:
        store.remember(old, domain="delivery", slot="total_price", value=30, strength="hard", operator="lte")
        candidate.pop("evidence_id")
        store.remember(newer, **candidate)
        assert store.active("u1", strength="hard")[0].value == 30


def test_soft_reserved_slot_cannot_override_hard():
    candidate = extract(row(strength="soft")).candidates[0]
    assert candidate["slot"] == "proposed.total_price"


@pytest.mark.parametrize("updates", [
    {"evidence_id": "fabricated"}, {"quote": "budget is 300"}, {"domain": "ota"},
    {"certainty": "inferred"}, {"confidence": 0.3}, {"confidence": True},
    {"uncertain": True}, {"operator": "execute"}, {"value": float("nan")},
    {"strength": "mandatory"}, {"slot": "room_type", "operator": "eq"},
    {"slot": "proposed.total_price"}, {"value": True},
    {"value": -3}, {"slot": "allergens", "operator": "excludes", "value": True},
])
def test_invalid_candidates_are_rejected(updates):
    result = extract(row(**updates))
    assert result.candidates == []
    assert result.diagnostics == [{"code": "candidate_rejected"}]


def test_multilingual_soft_slots_require_original_quotation():
    evidence = Evidence("e1", "u1", "我出差时喜欢靠窗的房间，安静一些。", 10)
    result = extract(row(quote=evidence.text, slot="room_preference", value="quiet room near window",
                         strength="soft", operator="eq"), evidence)
    assert result.candidates[0]["slot"] == "room_preference"
    assert result.candidates[0]["quote"] == evidence.text
    assert result.cost is None and result.usage is None


def test_only_one_user_and_only_user_source_can_reach_generator():
    calls = []
    extractor = EvidenceExtractor(lambda payload: calls.append(payload))
    with pytest.raises(ValueError, match="one user"):
        extractor.extract([(Evidence("a", "u1", "text", 1), "global"),
                           (Evidence("b", "u2", "text", 1), "global")])
    with pytest.raises(ValueError, match="user evidence"):
        extractor.extract([(Evidence("a", "u1", "order history", 1, "behavior"), "global")])
    assert calls == []


def test_provider_receives_evidence_fields_only_and_unknown_cost_stays_unknown():
    calls = []
    def generator(payload):
        calls.append(payload)
        return {"preferences": []}
    result = EvidenceExtractor(generator).extract([(Evidence("e1", "u1", "personal request", 1), "global")])
    assert set(calls[0]) == {"system", "evidence"}
    assert set(calls[0]["evidence"][0]) == {"evidence_id", "text", "timestamp", "domain"}
    assert result.cost is None and result.usage is None


def test_malformed_output_keeps_paid_usage():
    extractor = EvidenceExtractor(lambda payload: {"preferences": "invalid", "cost": 0.1})
    result = extractor.extract([(Evidence("e1", "u1", "text", 1), "global")])
    assert result.candidates == [] and result.cost == 0.1
    assert result.diagnostics[0]["code"] == "malformed_preferences"


def test_conflicting_same_evidence_slot_candidates_are_both_removed():
    evidence = Evidence("e1", "u1", "I like quiet hotels", 1)
    first = row(quote=evidence.text, slot="hotel_style", value="quiet", operator="eq", strength="soft")
    second = {**first, "value": "lively"}
    result = EvidenceExtractor(lambda p: {"preferences": [first, second]}).extract([(evidence, "delivery")])
    assert result.candidates == []
    assert result.diagnostics == [{"code": "contradictory_candidates"}]


def test_empty_batch_does_not_call_provider_and_overlong_batch_fails():
    calls = []
    extractor = EvidenceExtractor(lambda p: calls.append(p), max_input_chars=5)
    result = extractor.extract([])
    assert result.cost == 0 and not calls
    with pytest.raises(ValueError, match="split"):
        extractor.extract([(Evidence("e1", "u1", "more than five", 1), "global")])
    assert not calls


def test_history_is_persistent_user_isolated_and_tiered(tmp_path):
    path = tmp_path / "history.sqlite3"
    with EvidenceHistory(path) as history:
        history.add(Evidence("a", "u1", "quiet hotel", 1), "ota")
        history.add(Evidence("b", "u1", "quiet restaurant", 10), "instore")
        history.add(Evidence("c", "u1", "quiet places", 5), "global")
        history.add(Evidence("a", "u2", "private hotel preference", 100), "ota")
    with EvidenceHistory(path) as history:
        rows = history.retrieve("u1", "quiet", domain="ota", top_k=3)
        assert [r["id"] for r in rows] == ["a", "c", "b"]
        assert {r["user_id"] for r in rows} == {"u1"}
        history.reset("u1")
        assert history.retrieve("u1") == []
        assert len(history.retrieve("u2")) == 1


def test_behavior_history_is_only_soft_and_stale_evidence_can_be_excluded():
    with EvidenceHistory() as history:
        history.add(Evidence("old", "u1", "I like Italian food", 1), "delivery")
        history.add(Evidence("new", "u1", "I now prefer Sichuan food", 2), "delivery")
        history.add(Evidence("order", "u1", "Ordered noodles", 3, "behavior"), "delivery")
        rows = history.retrieve("u1", domain="delivery", inactive_evidence_ids=["old"])
        assert {r["id"] for r in rows} == {"new", "order"}
        assert next(r for r in rows if r["id"] == "order")["interpretation"] == "inferred_soft_only"
        rendered = history.render("u1", domain="delivery", max_chars=5)
        payload = json.loads(rendered.split("\n", 1)[1])
        assert sum(len(r["text"]) for r in payload) <= 5
        assert payload[0]["truncated"]


def test_history_replay_does_not_silently_overwrite_evidence():
    with EvidenceHistory() as history:
        evidence = Evidence("a", "u1", "original", 1)
        history.add(evidence)
        history.add(evidence)
        assert len(history.retrieve("u1")) == 1
        with pytest.raises(ValueError, match="collision"):
            history.add(Evidence("a", "u1", "changed", 1))
        with pytest.raises(ValueError):
            history.add(Evidence("b", "u1", "assistant claim", 1, "assistant"))
