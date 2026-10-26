import json

import pytest

from vita_agent.traces import export_failed_traces


def files(tmp_path, simulations, partition="train"):
    results, plan = tmp_path / "result.json", tmp_path / "plan.json"
    results.write_text(json.dumps({"simulations": simulations}))
    plan.write_text(json.dumps({"partition": partition, "runs": [{"result": str(results)}],
        "dataset": {"tasks": {"train": ["train-user"]}, "sha256": "dataset-sha"},
        "selected_task_ids": ["train-user"]}))
    return results, plan, tmp_path / "traces.jsonl"


def simulation(score, error=True, content="Hotel secret-customer-name not found", task="train-user"):
    return {"task_id": task, "trial": 0, "reward_info": {"reward": score}, "messages": [
        {"role": "tool", "name": "get_ota_hotel_info", "error": error, "content": content}]}


def test_observed_error_exported_without_private_messages(tmp_path):
    args = files(tmp_path, [simulation(.4)])
    report = export_failed_traces(*args)
    assert report["exported"] == 1
    text = args[2].read_text()
    assert "secret-customer-name" not in text
    trace = json.loads(text)
    assert trace["steps"][0]["violations"] == [{"code": "unknown_entity"}]
    assert trace["task_id"] == "train-user" and trace["domain"] == "ota"


def test_low_score_alone_and_unknown_errors_never_invent_lessons(tmp_path):
    args = files(tmp_path, [simulation(.1, error=False), simulation(0, content="Something went wrong"), simulation(1)])
    report = export_failed_traces(*args)
    assert report["exported"] == 0
    assert report["unknown_tool_errors_skipped"] == 1
    assert report["unsuccessful_without_known_tool_errors"] == 2
    assert report["successful_skipped"] == 1


def test_test_plan_and_foreign_users_rejected(tmp_path):
    args = files(tmp_path, [simulation(0)], partition="test")
    with pytest.raises(ValueError, match="training"):
        export_failed_traces(*args)
    args = files(tmp_path, [simulation(0, task="test-user")])
    with pytest.raises(ValueError, match="outside"):
        export_failed_traces(*args)


def test_export_does_not_overwrite_existing_traces(tmp_path):
    args = files(tmp_path, [simulation(0)])
    export_failed_traces(*args)
    with pytest.raises(FileExistsError):
        export_failed_traces(*args)
