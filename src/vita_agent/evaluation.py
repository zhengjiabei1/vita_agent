"""Strict paired reporting for official VitaBench 2.0 ``Results`` JSON files.

Scores are only compared when every declared user/trial/subtask is present. A
missing/crashed run is an error, not an implicitly discarded observation. The
caller must finish evaluation or explicitly export a complete, failure-scored
result before comparing. Costs remain unknown if any measured cost is missing.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import random
from statistics import fmean
from typing import Iterable


def _number(value, name: str, *, score: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if value < 0 or (score and value > 1):
        raise ValueError(f"{name} is outside its allowed range")
    return float(value)


def _positive_int(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _get(data: dict, dotted: str):
    for name in dotted.split("."):
        if not isinstance(data, dict) or name not in data:
            return None
        data = data[name]
    return data


def _load(path: str | Path) -> dict:
    with Path(path).open(encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, dict) or not isinstance(data.get("info"), dict):
        raise ValueError("Expected official VitaBench Results JSON with info, tasks and simulations")
    trials = _positive_int(data["info"].get("num_trials"), "info.num_trials")
    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("Expected nonempty declared tasks cohort")
    users = []
    for task in tasks:
        if not isinstance(task, dict) or not isinstance(task.get("id"), str) or not task["id"]:
            raise ValueError("Each declared task needs a nonempty string id")
        users.append(task["id"])
    if len(users) != len(set(users)):
        raise ValueError("Duplicate task IDs in expected cohort")
    simulations = data.get("simulations")
    if not isinstance(simulations, list):
        raise ValueError("simulations must be a list")
    rows = {}
    structures = {}
    for simulation in simulations:
        if not isinstance(simulation, dict):
            raise ValueError("Each simulation must be an object")
        user, trial = simulation.get("task_id"), simulation.get("trial")
        if not isinstance(user, str) or user not in users:
            raise ValueError("Simulation task_id is outside the declared cohort")
        if isinstance(trial, bool) or not isinstance(trial, int) or not 0 <= trial < trials:
            raise ValueError("Simulation trial must be an explicit zero-based index")
        key = (user, trial)
        if key in rows:
            raise ValueError(f"Duplicate simulation for task/trial {key}")
        reward_info = simulation.get("reward_info")
        if not isinstance(reward_info, dict):
            raise ValueError(f"Missing evaluation for task/trial {key}; no complete-case dropping allowed")
        reward = _number(reward_info.get("reward"), "reward", score=True)
        detail = reward_info.get("info") or {}
        if not isinstance(detail, dict):
            raise ValueError("reward_info.info must be an object")
        subtasks = None
        tags = None
        if "num_subtasks" in detail or "subtask_rewards" in detail:
            count = _positive_int(detail.get("num_subtasks"), "num_subtasks")
            expected = {f"subtask_{i}_reward" for i in range(count)}
            breakdown = detail.get("subtask_rewards")
            if not isinstance(breakdown, dict) or set(breakdown) != expected:
                raise ValueError(f"Incomplete subtask rewards for {key}; evaluate all subtasks explicitly")
            if "num_evaluated" in detail and detail["num_evaluated"] != count:
                raise ValueError(f"Not all subtasks were evaluated for {key}")
            subtasks = [_number(breakdown[f"subtask_{i}_reward"], "subtask reward", score=True) for i in range(count)]
            if not math.isclose(reward, fmean(subtasks), rel_tol=1e-9, abs_tol=1e-9):
                raise ValueError(f"Aggregate reward is not the mean of complete subtask rewards for {key}")
            tags_map = detail.get("subtask_skill_tested")
            if tags_map is not None:
                if not isinstance(tags_map, dict) or set(tags_map) != {f"subtask_{i}" for i in range(count)}:
                    raise ValueError(f"Incomplete subtask skill tags for {key}")
                tags = []
                for i in range(count):
                    values = tags_map[f"subtask_{i}"]
                    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                        raise ValueError("Subtask skill tags must be lists of strings")
                    tags.append(tuple(sorted(set(values))))
            structure = (count, tags)
            if user in structures and structures[user] != structure:
                raise ValueError(f"Subtask cohort or tags changed across trials for {user}")
            structures[user] = structure
        elif _get(data["info"], "environment_info.domain_name") == "personalization":
            raise ValueError(f"Missing subtask evaluation details for personalization task {key}")
        cost = simulation.get("agent_cost")
        if cost is not None:
            cost = _number(cost, "agent_cost")
        rows[key] = {"reward": reward, "cost": cost, "subtasks": subtasks, "tags": tags, "seed": simulation.get("seed")}
    expected_keys = {(user, trial) for user in users for trial in range(trials)}
    if set(rows) != expected_keys:
        raise ValueError(f"Incomplete task/trial cohort: expected {len(expected_keys)}, found {len(rows)}; failed/missing runs cannot be silently omitted")
    return {"info": data["info"], "users": sorted(users), "trials": trials, "rows": rows}


def _bootstrap(user_deltas: list[float], samples: int, seed: int) -> list[float] | None:
    if len(user_deltas) < 2:
        return None
    rng = random.Random(seed)
    estimates = sorted(fmean(rng.choices(user_deltas, k=len(user_deltas))) for _ in range(samples))
    def quantile(q: float) -> float:
        position = (samples - 1) * q
        low, high = math.floor(position), math.ceil(position)
        return estimates[low] * (high - position) + estimates[high] * (position - low) if low != high else estimates[low]
    return [quantile(0.025), quantile(0.975)]


def compare_runs(baseline_path: str | Path, candidate_path: str | Path, *,
                 bootstrap_samples: int = 1000, seed: int = 42) -> dict:
    """Compare complete paired official runs without importing the benchmark.

    The primary reward gives each user equal weight, averaging their trials and
    subtasks. The update subset is micro-averaged over tagged subtask/trial pairs.
    Bootstrap resamples users, keeping all within-user observations together.
    ``agent_cost`` is upstream's recorded agent cost; unknown cost is never zero.
    """
    _positive_int(bootstrap_samples, "bootstrap_samples")
    baseline, candidate = _load(baseline_path), _load(candidate_path)
    base_currency = baseline["info"].get("cost_currency")
    new_currency = candidate["info"].get("cost_currency")
    if base_currency != new_currency:
        raise ValueError("Cost currencies must match; never compare CNY and USD or unspecified currency")
    if baseline["users"] != candidate["users"] or baseline["trials"] != candidate["trials"]:
        raise ValueError("Baseline and candidate must have identical declared user/trial cohorts")
    checked, unverified = [], []
    for field in ("git_commit", "seed", "max_steps", "max_errors", "agent_info.llm", "agent_info.llm_args",
                  "user_info.llm", "user_info.llm_args", "environment_info.domain_name",
                  "evaluator_info.llm", "evaluator_info.llm_args"):
        left, right = _get(baseline["info"], field), _get(candidate["info"], field)
        if left is None or right is None:
            unverified.append(field)
        elif left != right:
            raise ValueError(f"Incomparable run setting: {field}")
        else:
            checked.append(field)
    keys = sorted(baseline["rows"])
    update_base, update_new = [], []
    tags_available = True
    for key in keys:
        left, right = baseline["rows"][key], candidate["rows"][key]
        if left["seed"] is not None and right["seed"] is not None:
            if left["seed"] != right["seed"]:
                raise ValueError(f"Different simulation seeds for {key}")
        else:
            unverified.append("simulation.seed")
        if (left["subtasks"] is None) != (right["subtasks"] is None):
            raise ValueError("Subtask detail availability differs across runs")
        if left["subtasks"] is not None and len(left["subtasks"]) != len(right["subtasks"]):
            raise ValueError(f"Different subtask cohorts for {key}")
        if left["tags"] != right["tags"]:
            raise ValueError(f"Subtask skill tags differ across runs for {key}")
        if left["tags"] is None:
            tags_available = False
        else:
            for index, tags in enumerate(left["tags"]):
                if "update" in tags:
                    update_base.append(left["subtasks"][index])
                    update_new.append(right["subtasks"][index])
    base_mean = fmean(baseline["rows"][key]["reward"] for key in keys)
    new_mean = fmean(candidate["rows"][key]["reward"] for key in keys)
    deltas = [fmean(candidate["rows"][(user, trial)]["reward"] - baseline["rows"][(user, trial)]["reward"]
                    for trial in range(baseline["trials"])) for user in baseline["users"]]
    base_costs = [baseline["rows"][key]["cost"] for key in keys]
    new_costs = [candidate["rows"][key]["cost"] for key in keys]
    costs_known = all(value is not None for value in base_costs + new_costs)
    base_total = math.fsum(base_costs) if costs_known else None
    new_total = math.fsum(new_costs) if costs_known else None
    overhead = (new_total / base_total - 1) * 100 if costs_known and base_total > 0 else None
    update = None
    if tags_available and update_base:
        update = {"pairs": len(update_base), "baseline_mean": fmean(update_base), "candidate_mean": fmean(update_new),
                  "delta": fmean(update_new) - fmean(update_base), "aggregation": "micro mean over update-tagged subtask/trial pairs"}
    return {
        "schema_version": 1,
        "cohort": {"users": len(baseline["users"]), "trials_per_user": baseline["trials"], "paired_runs": len(keys), "complete": True},
        "reward": {"baseline_mean": base_mean, "candidate_mean": new_mean, "delta": new_mean - base_mean,
                   "delta_percentage_points": (new_mean - base_mean) * 100,
                   "aggregation": "equal user weight; mean across trials of each user's mean subtask reward",
                   "paired_user_bootstrap_95ci": _bootstrap(deltas, bootstrap_samples, seed),
                   "bootstrap_samples": bootstrap_samples, "bootstrap_seed": seed},
        "update_subset": update,
        "update_subset_status": "available" if update is not None else ("no_update_tasks" if tags_available else "missing_skill_tags"),
        "cost": {"currency": base_currency or "unspecified", "baseline_agent_total": base_total, "candidate_agent_total": new_total,
                 "overhead_percent": overhead, "all_costs_recorded": costs_known,
                 "status": "unknown" if not costs_known else ("zero_baseline" if base_total == 0 else "available"),
                 "scope": "upstream recorded agent_cost only; excludes unrecorded memory, offline distillation, user and evaluator costs"},
        "settings_checked": checked, "settings_unverified": sorted(set(unverified)),
        "limitations": ["No benchmark score or resume claim is assumed by this report.",
                        "A bootstrap interval describes these users, not proof of generalization or causality.",
                        "For skill-only gain compare frozen-bank versus no-bank with all other settings fixed.",
                        "Inspect generation retry and memory cost accounting before reporting total overhead."]
                        + (["Too few users for a user-level confidence interval."] if len(deltas) < 2 else []),
    }


def split_users(user_ids: Iterable[str], seed: int = 42, train_fraction: float = 0.6,
                dev_fraction: float = 0.2) -> dict[str, list[str]]:
    """Deterministic user-level split; all subtasks/trials for a user stay together."""
    ids = list(user_ids)
    if any(not isinstance(value, str) or not value for value in ids) or len(ids) != len(set(ids)):
        raise ValueError("user_ids must contain unique nonempty strings")
    if len(ids) < 3:
        raise ValueError("At least three users are required for nonempty train/dev/test splits")
    for fraction in (train_fraction, dev_fraction):
        if isinstance(fraction, bool) or not isinstance(fraction, (int, float)) or not math.isfinite(fraction) or fraction <= 0:
            raise ValueError("Split fractions must be finite positive numbers")
    if train_fraction + dev_fraction >= 1:
        raise ValueError("Split fractions must leave a positive test portion")
    ids.sort()
    random.Random(seed).shuffle(ids)
    train_count = max(1, int(len(ids) * train_fraction))
    dev_count = max(1, int(len(ids) * dev_fraction))
    if train_count + dev_count >= len(ids):
        raise ValueError("Too few users for the requested fractions")
    return {"train": sorted(ids[:train_count]), "dev": sorted(ids[train_count:train_count + dev_count]),
            "test": sorted(ids[train_count + dev_count:])}
