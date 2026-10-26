import hashlib
import json

import pytest

from vita_agent.skill_reflection import reflect_training_results, freeze_reflected_bank, load_reflected_bank


@pytest.fixture
def inputs(tmp_path):
    dataset = tmp_path / "tasks.json"
    dataset.write_text(json.dumps([{"id": "train-one", "user_id": "private-user", "user_profile": {"name": "Alice"},
        "subtasks": [{"domain": "delivery", "instruction": "Order dinner", "rubric": ["Respect the latest dietary preference"]}]}]))
    result = tmp_path / "results.json"
    result.write_text(json.dumps({"simulations": [{"task_id": "train-one", "trial": 0, "reward_info": {"reward": 0},
        "messages": [{"role": "user", "content": "I changed my dietary preference yesterday."},
                     {"role": "assistant", "content": "I will use the older preference to choose dinner.",
                      "raw_data": {"secret": "MUST_NOT_LEAVE"}}]}]}))
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"partition": "train", "selected_task_ids": ["train-one"],
        "runs": [{"result": str(result)}], "dataset": {"path": str(dataset),
         "sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(), "tasks": {"train": ["train-one"], "test": ["test-one"]}}}))
    return result, plan


def candidate():
    return {"level": "task", "domain": "delivery", "tool": "action",
            "lesson": "Check the most recent preference evidence before choosing a candidate and verify that older conflicting evidence is superseded.",
            "citations": [{"message_index": 1, "quote": "use the older preference"}]}


def test_real_trace_reflection_is_only_a_cited_hypothesis(inputs, tmp_path):
    def generator(payload):
        assert payload["observed_reward"] == 0
        assert "MUST_NOT_LEAVE" not in json.dumps(payload)
        assert "TRAINING" in payload["system"]
        return {"skills": [candidate()], "cost": 0.01, "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    report = reflect_training_results(*inputs, generator)
    assert len(report.skills) == 1 and report.costs == [0.01]
    assert report.skills[0].failure_codes == ()
    assert report.skills[0].trials == 0
    path = tmp_path / "frozen.json"
    digest = freeze_reflected_bank(path, report.candidates, excluded_task_ids=["test-one"])
    bank = load_reflected_bank(path)
    assert bank.digest == digest
    assert len(bank.retrieve("preference", "delivery")) == 1
    assert "use the older preference" not in path.read_text()
    with pytest.raises(RuntimeError):
        bank.record_outcome(bank.list_skills()[0].id, True)
    with pytest.raises(FileExistsError):
        freeze_reflected_bank(path, report.candidates)


@pytest.mark.parametrize("mutation", [
    lambda x: x["citations"][0].update(quote="fabricated evidence"),
    lambda x: x["citations"][0].update(message_index=99),
    lambda x: x.update(lesson="Always choose Alice's preferred dinner before placing an order."),
    lambda x: x.update(lesson="Always spend less than 30 on this user's dinner order."),
    lambda x: x.update(lesson="Book room_123 from the previous successful hotel booking."),
    lambda x: x.update(domain="ota"),
    lambda x: x.update(citations=[]),
])
def test_bad_citations_and_source_specific_lessons_rejected(inputs, mutation):
    item = candidate()
    mutation(item)
    report = reflect_training_results(*inputs, lambda p: {"skills": [item], "cost": None})
    assert not report.skills
    assert report.diagnostics == [{"code": "candidate_rejected"}]
    assert report.costs == [None]


def test_no_grounded_lesson_is_valid_and_does_not_invent_a_failure_cause(inputs):
    report = reflect_training_results(*inputs, lambda p: {"skills": [], "cost": 0.02})
    assert report.skills == [] and report.costs == [0.02]


def test_reject_heldout_plans_before_model_call(inputs):
    result, plan = inputs
    data = json.loads(plan.read_text())
    data["partition"] = "test"
    plan.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="training plan"):
        reflect_training_results(result, plan, lambda p: pytest.fail("No model call allowed"))


def test_freeze_excludes_heldout_provenance(inputs, tmp_path):
    report = reflect_training_results(*inputs, lambda p: {"skills": [candidate()]})
    with pytest.raises(ValueError, match="held-out"):
        freeze_reflected_bank(tmp_path / "bad.json", report.candidates, excluded_task_ids=["train-one"])


def test_corrupted_snapshot_and_false_outcome_claim_rejected(inputs, tmp_path):
    report = reflect_training_results(*inputs, lambda p: {"skills": [candidate()]})
    path = tmp_path / "frozen.json"
    freeze_reflected_bank(path, report.candidates)
    document = json.loads(path.read_text())
    document["payload"]["candidates"][0]["skill"]["trials"] = 3
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="integrity"):
        load_reflected_bank(path)
    from vita_agent.skill_reflection import _hash
    document["sha256"] = _hash(document["payload"])
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="claim outcomes"):
        load_reflected_bank(path)


def test_changed_dataset_is_rejected(inputs):
    result, plan = inputs
    data = json.loads(plan.read_text())
    data["dataset"]["sha256"] = "0" * 64
    plan.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="hash changed"):
        reflect_training_results(result, plan, lambda p: pytest.fail("No model call allowed"))
