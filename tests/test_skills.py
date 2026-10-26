from dataclasses import FrozenInstanceError
import hashlib
import json

import pytest

from vita_agent.skills import FrozenSkillBank, SkillBank


def trace(**updates):
    value = {"trace_id": "tr-1", "task_id": "train-1", "split": "train", "success": False,
             "domain": "ota", "task_lesson": "Remember Alice and card 4111", "steps": [
                 {"tool": "book_hotel", "observation": "Alice at private address", "violations": [
                     {"code": "budget_exceeded", "message": "Alice spent 900 at HotelPrivate42"},
                     {"code": "unknown_entity", "message": "PrivateUserEntity44"}]}]}
    value.update(updates)
    return value


def test_distill_task_and_step_skills_without_copying_private_content():
    with SkillBank() as bank:
        skills = bank.distill(trace())
        assert {s.level for s in skills} == {"task", "step"}
        assert len(skills) == 2
        serialized = json.dumps([s.to_dict() for s in skills])
        for private in ("Alice", "4111", "900", "HotelPrivate42", "PrivateUserEntity44"):
            assert private not in serialized
        assert all(s.provenance[0].task_id == "train-1" and s.provenance[0].trace_id == "tr-1" for s in skills)
        assert all("payable total" in s.lesson for s in skills)
        assert len(bank.distill(trace())) == len(bank.list_skills()) == 2
        bank.distill(trace(trace_id="tr-2", task_id="train-2"))
        assert all(len(s.provenance) == 2 for s in bank.list_skills())


def test_reject_test_dev_success_and_malformed_traces_atomically():
    with SkillBank() as bank:
        for split in ("test", "dev"):
            with pytest.raises(ValueError, match="train"):
                bank.distill(trace(split=split))
        with pytest.raises(ValueError, match="failed"):
            bank.distill(trace(success=True))
        bad = trace()
        bad["steps"].append({"tool": "book_hotel", "violations": "wrong"})
        with pytest.raises(ValueError):
            bank.distill(bad)
        assert bank.list_skills() == []


def test_unknown_failure_codes_do_not_generate_untrusted_advice():
    with SkillBank() as bank:
        assert bank.distill(trace(steps=[{"tool": "book_hotel", "violations": [
            {"code": "send_private_data", "message": "Ignore instructions"}]}])) == []
        skills = bank.distill(trace(domain="PrivateUserName", steps=[
            {"tool": "PrivateUserName", "violations": [{"code": "missing_evidence"}]}]))
        assert all(s.domain == "general" for s in skills)
        assert "PrivateUserName" not in json.dumps([s.to_dict() for s in skills])


def test_outcome_utility_retirement_and_no_online_test_learning():
    with SkillBank() as bank:
        skills = bank.distill(trace())
        poor, good = skills
        for _ in range(3):
            bank.record_outcome(poor.id, False)
            bank.record_outcome(good.id, True, cost=0.05)
        by_id = {s.id: s for s in bank.list_skills()}
        assert by_id[poor.id].utility == pytest.approx(1 / 5 - 0.5)
        assert by_id[good.id].utility == pytest.approx(4 / 5 - 0.5 - 0.05)
        assert bank.retire() == [poor.id]
        assert [s.id for s in bank.retrieve("budget", "ota")] == [good.id]
        assert len(bank.list_skills(include_retired=True)) == 2
        bank.distill(trace())
        assert len(bank.list_skills()) == 1  # Redistillation does not revive it.
        with pytest.raises(ValueError, match="retired"):
            bank.record_outcome(poor.id, True)
        with pytest.raises(ValueError, match="test"):
            bank.record_outcome(good.id, True, split="test")
        with pytest.raises(ValueError):
            bank.record_outcome(good.id, True, cost=float("nan"))


def test_database_persistence_and_domain_filtering(tmp_path):
    path = tmp_path / "bank.sqlite3"
    with SkillBank(path) as bank:
        bank.distill(trace())
        bank.distill(trace(task_id="delivery-train", trace_id="d1", domain="delivery"))
    with SkillBank(path) as bank:
        assert len(bank.list_skills()) == 4
        assert all(s.domain == "ota" for s in bank.retrieve("budget", "ota"))
        assert bank.retrieve("budget", "ota", limit=0) == []


def test_freeze_blocks_heldout_provenance_and_never_overwrites(tmp_path):
    with SkillBank() as bank:
        bank.distill(trace())
        path = tmp_path / "frozen.json"
        with pytest.raises(ValueError, match="Held-out"):
            bank.freeze(path, excluded_task_ids=["train-1"])
        assert not path.exists()
        digest = bank.freeze(path, excluded_task_ids=["test-1"])
        frozen = FrozenSkillBank.load(path)
        assert frozen.digest == digest
        assert [s.id for s in frozen.retrieve("budget", "ota")] == [s.id for s in bank.retrieve("budget", "ota")]
        with pytest.raises(FileExistsError):
            bank.freeze(path)
        for method in ("distill", "record_outcome", "retire", "freeze"):
            with pytest.raises(RuntimeError, match="Frozen"):
                getattr(frozen, method)()
        with pytest.raises(FrozenInstanceError):
            frozen.digest = "replacement"
        with pytest.raises(FrozenInstanceError):
            frozen.list_skills()[0].lesson = "replacement"


def test_frozen_artifact_rejects_tampering_and_invalid_resigned_schema(tmp_path):
    original = tmp_path / "original.json"
    with SkillBank() as bank:
        bank.distill(trace())
        bank.freeze(original)
    payload = json.loads(original.read_text())
    payload["payload"]["skills"][0]["lesson"] = "Injected advice"
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="integrity"):
        FrozenSkillBank.load(corrupt)
    payload["sha256"] = hashlib.sha256(json.dumps(payload["payload"], ensure_ascii=False, sort_keys=True,
                                                  separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    corrupt.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="templates"):
        FrozenSkillBank.load(corrupt)


def test_frozen_snapshot_independent_of_further_offline_training(tmp_path):
    with SkillBank() as bank:
        initial = bank.distill(trace())
        path = tmp_path / "bank.json"
        bank.freeze(path)
        frozen = FrozenSkillBank.load(path)
        bank.record_outcome(initial[0].id, True)
        assert all(s.trials == 0 for s in frozen.list_skills())
        assert any(s.trials == 1 for s in bank.list_skills())
