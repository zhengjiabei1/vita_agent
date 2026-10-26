from copy import deepcopy
import json

import pytest

from vita_agent.evaluation import compare_runs, split_users


def fixture(users=("u1", "u2"), trials=2):
    return {
        "info": {"num_trials": trials, "git_commit": "pinned", "seed": 42, "max_steps": 50,
                 "max_errors": 3, "agent_info": {"llm": "model", "llm_args": {"temperature": 0}},
                 "user_info": {"llm": "user-model", "llm_args": {}},
                 "environment_info": {"domain_name": "personalization"}},
        "tasks": [{"id": user} for user in users],
        "simulations": [
            {"task_id": user, "trial": trial, "seed": 100 + trial, "agent_cost": 1.0,
             "reward_info": {"reward": 0.5, "info": {"num_subtasks": 2, "num_evaluated": 2,
                "subtask_rewards": {"subtask_0_reward": 0.4, "subtask_1_reward": 0.6},
                "subtask_skill_tested": {"subtask_0": ["update"], "subtask_1": []}}}}
            for user in users for trial in range(trials)],
    }


def compare(tmp_path, baseline, candidate, **kwargs):
    first, second = tmp_path / "baseline.json", tmp_path / "candidate.json"
    first.write_text(json.dumps(baseline))
    second.write_text(json.dumps(candidate))
    return compare_runs(first, second, **kwargs)


def test_paired_metrics_update_and_cost(tmp_path):
    baseline = fixture()
    candidate = deepcopy(baseline)
    for simulation in candidate["simulations"]:
        simulation["reward_info"]["reward"] = 0.6
        simulation["reward_info"]["info"]["subtask_rewards"]["subtask_0_reward"] = 0.6
        simulation["agent_cost"] = 1.086
    result = compare(tmp_path, baseline, candidate)
    assert result["cohort"] == {"users": 2, "trials_per_user": 2, "paired_runs": 4, "complete": True}
    assert result["reward"]["delta_percentage_points"] == pytest.approx(10)
    assert result["cost"]["overhead_percent"] == pytest.approx(8.6)
    assert result["update_subset"]["delta"] == pytest.approx(0.2)
    assert result["reward"]["paired_user_bootstrap_95ci"] == pytest.approx([0.1, 0.1])
    assert "evaluator_info.llm" in result["settings_unverified"]
    assert result == compare(tmp_path, baseline, candidate)


def test_cny_cost_is_labelled_and_mixed_currencies_rejected(tmp_path):
    baseline, candidate = fixture(), fixture()
    baseline["info"]["cost_currency"] = candidate["info"]["cost_currency"] = "CNY"
    assert compare(tmp_path, baseline, candidate)["cost"]["currency"] == "CNY"
    candidate["info"]["cost_currency"] = "USD"
    with pytest.raises(ValueError, match="currencies"):
        compare(tmp_path, baseline, candidate)


@pytest.mark.parametrize("change", [
    lambda d: d["simulations"].pop(),
    lambda d: d["simulations"].append(deepcopy(d["simulations"][0])),
    lambda d: d["simulations"][0].update(reward_info=None),
    lambda d: d["simulations"][0]["reward_info"]["info"]["subtask_rewards"].pop("subtask_1_reward"),
    lambda d: d["simulations"][0]["reward_info"]["info"].update(num_evaluated=1),
    lambda d: d["simulations"][0]["reward_info"].update(reward=0.9),
    lambda d: d["simulations"][0].update(trial=True),
    lambda d: d["simulations"][0].update(agent_cost=float("nan")),
    lambda d: d["tasks"].append({"id": "u1"}),
])
def test_incomplete_or_corrupt_runs_rejected(tmp_path, change):
    baseline, candidate = fixture(), fixture()
    change(candidate)
    with pytest.raises(ValueError):
        compare(tmp_path, baseline, candidate)


@pytest.mark.parametrize("field,value", [("seed", 43), ("max_steps", 20), ("git_commit", "different")])
def test_config_mismatch_rejected(tmp_path, field, value):
    baseline, candidate = fixture(), fixture()
    candidate["info"][field] = value
    with pytest.raises(ValueError, match="setting"):
        compare(tmp_path, baseline, candidate)


def test_seed_and_tag_mismatch_rejected(tmp_path):
    baseline, candidate = fixture(), fixture()
    candidate["simulations"][0]["seed"] = 77
    with pytest.raises(ValueError, match="seed"):
        compare(tmp_path, baseline, candidate)
    candidate = fixture()
    for simulation in candidate["simulations"]:
        simulation["reward_info"]["info"]["subtask_skill_tested"]["subtask_0"] = []
    with pytest.raises(ValueError, match="tags"):
        compare(tmp_path, baseline, candidate)


def test_unknown_cost_is_not_free(tmp_path):
    baseline, candidate = fixture(), fixture()
    candidate["simulations"][0]["agent_cost"] = None
    cost = compare(tmp_path, baseline, candidate)["cost"]
    assert cost["overhead_percent"] is None
    assert cost["baseline_agent_total"] is None
    assert cost["status"] == "unknown"


def test_zero_baseline_and_single_user(tmp_path):
    baseline, candidate = fixture(users=("one",)), fixture(users=("one",))
    for simulation in baseline["simulations"]:
        simulation["agent_cost"] = 0
    result = compare(tmp_path, baseline, candidate)
    assert result["cost"]["overhead_percent"] is None
    assert result["cost"]["status"] == "zero_baseline"
    assert result["reward"]["paired_user_bootstrap_95ci"] is None


def test_missing_tags_make_update_score_unavailable(tmp_path):
    baseline, candidate = fixture(), fixture()
    for dataset in (baseline, candidate):
        for simulation in dataset["simulations"]:
            simulation["reward_info"]["info"].pop("subtask_skill_tested")
    result = compare(tmp_path, baseline, candidate)
    assert result["update_subset"] is None
    assert result["update_subset_status"] == "missing_skill_tags"


def test_different_expected_cohorts_rejected(tmp_path):
    with pytest.raises(ValueError, match="cohorts"):
        compare(tmp_path, fixture(), fixture(users=("u1", "u3")))


def test_split_is_user_grouped_disjoint_and_reproducible():
    ids = [f"u{i}" for i in range(20)]
    result = split_users(ids)
    assert result == split_users(reversed(ids))
    assert len(result["train"]) == 12
    assert len(result["dev"]) == 4
    assert len(result["test"]) == 4
    assert set(sum(result.values(), [])) == set(ids)
    assert not set(result["train"]) & set(result["test"])


@pytest.mark.parametrize("ids", [["a", "a", "b"], ["a", "b"], ["a", "b", ""]])
def test_bad_split_input(ids):
    with pytest.raises(ValueError):
        split_users(ids)
