import json

import pytest

pytest.importorskip("yaml", reason="Official model config preflight needs the optional VitaBench dependencies")

from vita_agent.experiments import (
    UPSTREAM_COMMIT, build_plan, dataset_manifest, execute_plan, install_output_redaction,
    model_preflight, redact_settings, verify_plan,
)
from vita_agent.skills import SkillBank


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    dataset = tmp_path / "data" / "personalization" / "tasks.json"
    dataset.parent.mkdir(parents=True)
    records = [{"id": f"task-{i}", "user_id": f"user-{i}", "subtasks": [
        {"subtask_id": "0", "domain": "delivery", "instruction": "Lunch", "environment": {}}]} for i in range(10)]
    dataset.write_text(json.dumps(records))
    config = tmp_path / "models.yaml"
    config.write_text("default:\n  base_url: https://models.example/v1\n  api_key: ${VITA_TEST_KEY}\n"
                      "  max_tokens: 128\n  cost_1m_token_dollar:\n    prompt_price: 1.0\n    completion_price: 2.0\n"
                      "models:\n  - name: test-model\n")
    root = tmp_path / "project"
    (root / "src" / "vita_agent").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "src" / "vita_agent" / "test_source.py").write_text("# reviewed source\n")
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    def git_output(command, **kwargs):
        return UPSTREAM_COMMIT if "rev-parse" in command else ""
    monkeypatch.setattr("vita_agent.experiments.subprocess.check_output", git_output)
    monkeypatch.delenv("VITA_TEST_KEY", raising=False)
    return {"dataset": dataset, "model_config": config, "model": "test-model", "upstream": upstream,
            "project_root": root, "output": tmp_path / "experiment", "variants": ["baseline", "harness", "memory"]}


def test_user_split_is_deterministic_and_does_not_split_a_user(inputs):
    path = inputs["dataset"]
    records = json.loads(path.read_text())
    extra = dict(records[0], id="same-user-extra")
    records.append(extra)
    path.write_text(json.dumps(records))
    a = dataset_manifest(path)
    assert a == dataset_manifest(path)
    assert a["task_count"] == 11 and a["user_count"] == 10
    partitions = [{*v} for v in a["tasks"].values()]
    assert any({"task-0", "same-user-extra"} <= p for p in partitions)
    assert all(not x & y for i, x in enumerate(partitions) for y in partitions[i + 1:])


def test_short_max_steps_is_rejected_to_avoid_premature_termination_zeros(inputs):
    with pytest.raises(ValueError, match="premature-termination"):
        build_plan(**dict(inputs, max_steps=20))


def test_model_preflight_reports_names_not_secret_values(inputs):
    result = model_preflight(inputs["model_config"], "test-model", environment={"VITA_TEST_KEY": "private-test-value"})
    assert result["ready"] and result["required_variable_names"] == ["VITA_TEST_KEY"]
    assert "private-test-value" not in json.dumps(result)
    missing = model_preflight(inputs["model_config"], "test-model", environment={})
    assert missing["missing_variable_names"] == ["VITA_TEST_KEY"] and not missing["ready"]


def test_no_paid_calls_or_credentials_required_for_plan(inputs):
    plan = build_plan(**inputs)
    assert not plan["model"]["ready"]
    assert plan["partition"] == "dev" and len(plan["selected_user_ids"]) == 1
    assert all(r["upstream_arguments"][r["upstream_arguments"].index("--agent-llm") + 1] == "test-model" for r in plan["runs"])
    assert plan["runs"][0]["environment"]["VITA_ENABLE_GUARDS"] == "0"
    assert plan["runs"][1]["environment"]["VITA_ENABLE_GUARDS"] == "1"
    assert plan["runs"][0]["upstream_arguments"][-1] == "full_context"
    assert plan["runs"][1]["upstream_arguments"][-1] == "full_context"
    assert verify_plan(inputs["output"] / "plan.json")["dataset"]["sha256"] == plan["dataset"]["sha256"]


def test_execution_rejects_absent_credentials_and_does_not_create_run(inputs):
    build_plan(**inputs)
    with pytest.raises(ValueError, match="credentials"):
        execute_plan(inputs["output"] / "plan.json", budget_usd=1)
    assert not (inputs["output"] / "execution.json").exists()


def test_existing_plan_and_changed_sources_are_rejected(inputs):
    build_plan(**inputs)
    with pytest.raises(ValueError, match="empty"):
        build_plan(**inputs)
    (inputs["project_root"] / "src" / "vita_agent" / "test_source.py").write_text("# changed")
    with pytest.raises(ValueError, match="Implementation changed"):
        verify_plan(inputs["output"] / "plan.json")


def test_changed_dataset_rejected(inputs):
    build_plan(**inputs)
    inputs["dataset"].write_text(inputs["dataset"].read_text() + "\n")
    with pytest.raises(ValueError, match="Dataset changed"):
        verify_plan(inputs["output"] / "plan.json")


def test_skill_ablation_requires_training_provenance(inputs, tmp_path):
    data = dataset_manifest(inputs["dataset"])
    path = tmp_path / "bank.json"
    with SkillBank() as bank:
        bank.distill({"trace_id": "trace", "task_id": data["tasks"]["test"][0], "split": "train",
                      "success": False, "domain": "delivery", "steps": [{"tool": "order_food",
                      "violations": [{"code": "budget_exceeded"}]}]})
        bank.freeze(path)
    with pytest.raises(ValueError, match="training partition"):
        build_plan(**dict(inputs, variants=["skills"], skill_bank=path))


def test_valid_frozen_bank_used_read_only_and_full_uses_test(inputs, tmp_path):
    data = dataset_manifest(inputs["dataset"])
    path = tmp_path / "bank.json"
    with SkillBank() as bank:
        bank.distill({"trace_id": "trace", "task_id": data["tasks"]["train"][0], "split": "train",
                      "success": False, "domain": "delivery", "steps": [{"tool": "order_food",
                      "violations": [{"code": "budget_exceeded"}]}]})
        bank.freeze(path)
    plan = build_plan(**dict(inputs, variants=["baseline", "skills", "full"], phase="full", skill_bank=path))
    assert plan["partition"] == "test"
    assert plan["selected_task_ids"] == data["tasks"]["test"]
    assert plan["runs"][1]["environment"]["VITA_SKILL_BANK"] == str(path.resolve())
    assert plan["runs"][0]["environment"]["VITA_SKILL_BANK"] == ""


def test_memory_harness_diagnostic_variant_combines_structured_memory_and_guards(inputs):
    plan = build_plan(**dict(inputs, variants=["baseline", "memory_harness"]))
    memory_run = plan["runs"][1]
    assert memory_run["environment"]["VITA_ENABLE_GUARDS"] == "1"
    assert memory_run["upstream_arguments"][-2] == "--memory-class"
    assert memory_run["upstream_arguments"][-1] == "vita_agent.vitabench.StructuredPreferenceMemory"
    assert plan["skill_bank"] is None


def test_v2_reflected_bank_is_accepted_by_the_runner(inputs, tmp_path):
    from vita_agent.skill_reflection import ReflectedCandidate, _lesson_id, freeze_reflected_bank
    from vita_agent.skills import Provenance, Skill
    data = dataset_manifest(inputs["dataset"])
    lesson = ("Check the most recent preference evidence before choosing a candidate "
              "and verify that older conflicting evidence is superseded.")
    skill = Skill(_lesson_id("task", "delivery", "action", lesson), "task", "delivery", "action",
                  lesson, (), (Provenance(data["tasks"]["train"][0], "trace-v2"),))
    candidate = ReflectedCandidate(skill, "a" * 64, "b" * 64, ((1, "c" * 64),))
    path = tmp_path / "v2.json"
    freeze_reflected_bank(path, [candidate], excluded_task_ids=data["tasks"]["test"])
    plan = build_plan(**dict(inputs, variants=["baseline", "skills"], skill_bank=path))
    assert plan["skill_bank"]["payload_digest"] == json.loads(path.read_text())["sha256"]
    assert plan["runs"][1]["environment"]["VITA_SKILL_BANK"] == str(path.resolve())
    assert plan["model"]["billing"] == "paid"


@pytest.mark.parametrize("value", [None, 0, -1, True, float("nan"), "1"])
def test_invalid_budget_never_executes(value):
    with pytest.raises(ValueError, match="budget"):
        execute_plan("missing.json", budget_usd=value)


def test_wrong_dataset_structure_and_malformed_model_rejected(inputs):
    records = json.loads(inputs["dataset"].read_text())
    records[0]["user_id"] = ""
    inputs["dataset"].write_text(json.dumps(records))
    with pytest.raises(ValueError, match="user_id"):
        dataset_manifest(inputs["dataset"])
    inputs["model_config"].write_text("default: {}\nmodels: [{name: test-model}]\n")
    with pytest.raises(ValueError):
        model_preflight(inputs["model_config"], "test-model")


def test_execution_uses_budgeted_workers_and_stops_on_failure(inputs, monkeypatch):
    build_plan(**inputs)
    monkeypatch.setenv("VITA_TEST_KEY", "test-only-value")
    calls = []
    class Failure:
        returncode = 9
    def failed_worker(command, **kwargs):
        calls.append(command)
        return Failure()
    monkeypatch.setattr("vita_agent.experiments.subprocess.run", failed_worker)
    with pytest.raises(RuntimeError, match="Official evaluation failed"):
        execute_plan(inputs["output"] / "plan.json", budget_usd=5)
    assert len(calls) == 1
    assert calls[0][-2:] == [str(inputs["output"] / "plan.json"), "baseline"]
    execution = json.loads((inputs["output"] / "execution.json").read_text())
    assert execution["budget"] == 5 and execution["currency"] == "USD" and execution["budget_ledger"].endswith("budget.sqlite3")
    assert json.loads((inputs["output"] / "stopped.json").read_text())["exit_code"] == 9
    assert not (inputs["output"] / "comparisons.json").exists()
    with pytest.raises(FileExistsError):
        execute_plan(inputs["output"] / "plan.json", budget_usd=5)


def test_execution_refuses_existing_results(inputs, monkeypatch):
    build_plan(**inputs)
    monkeypatch.setenv("VITA_TEST_KEY", "test-only-value")
    (inputs["output"] / "baseline.json").write_text("existing")
    with pytest.raises(ValueError, match="overwrite"):
        execute_plan(inputs["output"] / "plan.json", budget_usd=5)
    assert not (inputs["output"] / "execution.json").exists()


def test_cny_tariff_and_currency_mismatch(inputs, monkeypatch):
    config = inputs["model_config"]
    config.write_text(config.read_text().replace("cost_1m_token_dollar", "cost_1m_token_cny"))
    plan = build_plan(**dict(inputs, phase="train", user_limit=2, memory_extraction="rules"))
    assert plan["model"]["currency"] == "CNY"
    assert len(plan["selected_user_ids"]) == 2 and plan["partition"] == "train"
    assert plan["runs"][0]["environment"]["VITA_MEMORY_EXTRACTION"] == "rules"
    monkeypatch.setenv("VITA_TEST_KEY", "test-only")
    with pytest.raises(ValueError, match="currency"):
        execute_plan(inputs["output"] / "plan.json", budget_usd=20)


def _write_free_config(path):
    path.write_text("default:\n  base_url: https://opencode.invalid/v1\n  api_key: ${VITA_TEST_KEY}\n"
                    "  max_tokens: 128\n  billing: free\n  free_quota:\n    unit: request\n"
                    "models:\n  - name: test-model\n")


def test_free_model_preflight_uses_quota_not_zero_prices(inputs):
    _write_free_config(inputs["model_config"])
    result = model_preflight(inputs["model_config"], "test-model",
                             environment={"VITA_TEST_KEY": "private-test-value"})
    assert result["billing"] == "free" and result["currency"] is None
    assert result["free_quota"] == {"unit": "request", "credit_per_request": None, "note": None}
    assert result["pricing_basis"] == "free_provider_quota"
    assert "private-test-value" not in json.dumps(result)


def test_free_model_rejects_token_tariff_and_bad_credit_unit(inputs):
    inputs["model_config"].write_text(
        "default:\n  base_url: https://opencode.invalid/v1\n  api_key: ${VITA_TEST_KEY}\n"
        "  max_tokens: 128\n  billing: free\n  cost_1m_token_dollar:\n"
        "    prompt_price: 0\n    completion_price: 0\n"
        "  free_quota:\n    unit: request\nmodels:\n  - name: test-model\n")
    with pytest.raises(ValueError, match="must not declare a token tariff"):
        model_preflight(inputs["model_config"], "test-model")
    inputs["model_config"].write_text(
        "default:\n  base_url: https://opencode.invalid/v1\n  api_key: ${VITA_TEST_KEY}\n"
        "  max_tokens: 128\n  billing: free\n  free_quota:\n    unit: credit\n"
        "models:\n  - name: test-model\n")
    with pytest.raises(ValueError, match="credit_per_request"):
        model_preflight(inputs["model_config"], "test-model")


def test_free_execution_requires_request_quota_and_shares_ledger(inputs, monkeypatch):
    _write_free_config(inputs["model_config"])
    build_plan(**dict(inputs, variants=["baseline"]))
    monkeypatch.setenv("VITA_TEST_KEY", "test-only-value")
    with pytest.raises(ValueError, match="max-requests"):
        execute_plan(inputs["output"] / "plan.json", budget_usd=1)
    class Failure:
        returncode = 9
    def failed_worker(command, **kwargs):
        return Failure()
    monkeypatch.setattr("vita_agent.experiments.subprocess.run", failed_worker)
    with pytest.raises(RuntimeError, match="Official evaluation failed"):
        execute_plan(inputs["output"] / "plan.json", max_requests=12,
                     budget_ledger=inputs["output"] / "shared.sqlite3")
    execution = json.loads((inputs["output"] / "execution.json").read_text())
    assert execution["mode"] == "requests" and execution["unit"] == "request"
    assert execution["budget"] == 12 and execution["currency"] == "FREE"
    assert execution["pricing_basis"] == "free_provider_quota"


def test_paid_execution_accepts_secondary_request_cap(inputs, monkeypatch):
    build_plan(**dict(inputs, variants=["baseline"]))
    monkeypatch.setenv("VITA_TEST_KEY", "test-only-value")
    class Failure:
        returncode = 9
    monkeypatch.setattr("vita_agent.experiments.subprocess.run", lambda *a, **k: Failure())
    with pytest.raises(RuntimeError, match="Official evaluation failed"):
        execute_plan(inputs["output"] / "plan.json", budget_usd=5, max_requests=7)
    execution = json.loads((inputs["output"] / "execution.json").read_text())
    assert execution["mode"] == "currency" and execution["request_limit"] == 7


def test_paid_model_rejects_request_quota(inputs, monkeypatch):
    build_plan(**dict(inputs, variants=["baseline"]))
    monkeypatch.setenv("VITA_TEST_KEY", "test-only-value")
    with pytest.raises(ValueError, match="budget"):
        execute_plan(inputs["output"] / "plan.json", max_requests=5)


def test_redaction_preserves_sampling_but_removes_credentials():
    value = {"api_key": "private-value", "max_tokens": 12, "temperature": 0,
             "headers": {"Authorization": "Bearer private-value"},
             "base_url": "https://name:private-value@service.example/v1?key=private-value"}
    cleaned = redact_settings(value)
    assert "private-value" not in json.dumps(cleaned)
    assert cleaned["max_tokens"] == 12 and cleaned["temperature"] == 0
    assert cleaned["base_url"] == "https://service.example/v1"
    assert value["api_key"] == "private-value"


def test_output_wrapper_does_not_change_inference_config():
    from copy import deepcopy
    from types import SimpleNamespace
    calls = []
    def get_info(domain, llm_args_agent=None):
        return {"domain": domain, "args": llm_args_agent}
    upstream = SimpleNamespace(get_info=get_info)
    display = SimpleNamespace(display_run_config=lambda value: calls.append(value))
    class Config:
        llm_args_agent = {"api_key": "private-value", "max_tokens": 10}
        llm_args_user = {"api_key": "private-value"}
        def model_copy(self, deep=False):
            return deepcopy(self)
    install_output_redaction(upstream, display)
    info = upstream.get_info("delivery", {"api_key": "private-value", "max_tokens": 10})
    assert info["args"]["api_key"] == "[REDACTED]"
    config = Config()
    display.display_run_config(config)
    assert calls[0].llm_args_agent["api_key"] == "[REDACTED]"
    assert config.llm_args_agent["api_key"] == "private-value"
