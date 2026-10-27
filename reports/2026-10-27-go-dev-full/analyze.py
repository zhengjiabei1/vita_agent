"""Summarize a completed run plan: per-user scores, arm means, paired bootstrap.

Usage: python reports/2026-10-27-go-dev-full/analyze.py <plan.json>

Only reads the plan and its result files; never calls a model. Scores come
straight from the official reward_info; nothing is imputed.
"""
from __future__ import annotations

import json
import random
from pathlib import Path
import sys
from statistics import mean


def load_users(dataset_path: str, task_ids: list[str]) -> dict[str, list[str]]:
    records = json.loads(Path(dataset_path).read_text(encoding="utf-8"))
    domains = {}
    for record in records:
        if record.get("id") in task_ids:
            domains[record["id"]] = [s.get("domain", "unknown") for s in record.get("subtasks", [])]
    return domains


def extract(path: Path, domains: dict[str, list[str]]) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    per_user_trials, subtask_trials, per_domain, terminations, tool_errors = {}, {}, {}, {}, {}
    for simulation in data["simulations"]:
        task_id = simulation["task_id"]
        reward_info = simulation["reward_info"]
        per_user_trials.setdefault(task_id, []).append(reward_info["reward"])
        detail = (reward_info.get("info") or {}).get("subtask_rewards") or {}
        for i in range(len(domains.get(task_id, []))):
            key = f"subtask_{i}_reward"
            if key not in detail:
                continue
            subtask_trials.setdefault(task_id, {}).setdefault(i, []).append(detail[key])
            domain = domains[task_id][i] if i < len(domains.get(task_id, [])) else "unknown"
            per_domain.setdefault(domain, []).append(detail[key])
        terminations.setdefault(task_id, []).append(simulation["termination_reason"])
        tool_errors.setdefault(task_id, []).append(
            sum(1 for m in simulation.get("messages", []) if m.get("role") == "tool" and m.get("error")))
    per_user = {task_id: mean(values) for task_id, values in per_user_trials.items()}
    subtasks = {task_id: [mean(values[i]) for i in sorted(values)]
                for task_id, values in subtask_trials.items()}
    return {"per_user": per_user, "subtasks": subtasks, "trials": len(data["simulations"]) // len(per_user),
            "domains": {k: {"n": len(v), "mean": mean(v)} for k, v in per_domain.items()},
            "terminations": terminations, "tool_errors": tool_errors}


def bootstrap_delta(users: list[str], base: dict, cand: dict, samples: int, seed: int) -> dict:
    rng = random.Random(seed)
    diffs = []
    for _ in range(samples):
        picked = [rng.choice(users) for _ in users]
        diffs.append(mean(cand[u] - base[u] for u in picked))
    diffs.sort()
    observed = mean(cand[u] - base[u] for u in users)
    return {"delta": observed,
            "ci95": [diffs[int(0.025 * samples)], diffs[int(0.975 * samples) - 1]],
            "p_delta_gt_0": sum(d > 0 for d in diffs) / samples,
            "p_delta_lt_0": sum(d < 0 for d in diffs) / samples}


def main(plan_path: str) -> int:
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    task_ids = plan["selected_task_ids"]
    domains = load_users(plan["dataset"]["path"], task_ids)
    arms = {}
    for run in plan["runs"]:
        result = Path(run["result"])
        if not result.exists():
            print(f"missing result: {result}", file=sys.stderr)
            return 1
        arms[run["variant"]] = extract(result, domains)
    users = sorted(task_ids)
    summary = {"plan": str(Path(plan_path).resolve()), "users": users,
               "variants": {name: {"mean": mean(arm["per_user"][u] for u in users),
                                   **arm} for name, arm in arms.items()}}
    summary["paired"] = {}
    for name in arms:
        if name == "baseline":
            continue
        summary["paired"][name] = bootstrap_delta(
            users, arms["baseline"]["per_user"], arms[name]["per_user"], 10000, plan["seed"])

    out = Path(plan_path).with_name("analysis.json")
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=float) + "\n", encoding="utf-8")

    lines = ["| arm | mean | " + " | ".join(users) + " |",
             "| --- | ---: | " + " | ".join("---:" for _ in users) + " |"]
    for name in plan_variants(plan):
        arm = arms[name]
        lines.append(f"| {name} | {mean(arm['per_user'][u] for u in users):.3f} | "
                     + " | ".join(f"{arm['per_user'][u]:.1f}" for u in users) + " |")
    print("\n".join(lines))
    for name, stats in summary["paired"].items():
        lo, hi = stats["ci95"]
        print(f"paired {name} - baseline: {stats['delta']:+.3f} (95% CI {lo:+.3f}..{hi:+.3f})")
    return 0


def plan_variants(plan: dict) -> list[str]:
    return [run["variant"] for run in plan["runs"]]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
