"""Analyze the salvaged split comparison.

Baseline comes from the completed 10-user x 2-trial run; the two guard arms
come from a later plan covering 8 of those users (a transient provider outage
aborted the 10-user guard runs). Only users present in both cohorts are paired;
no trial or user is imputed.

Usage:
  python reports/2026-10-27-go-dev-plain/analyze_split.py \
    --baseline artifacts/go-dev-plain-v1/baseline.json \
    --plan artifacts/go-dev-plain-v2/plan.json \
    --dataset artifacts/prefix2/personalization/tasks.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import random
from statistics import mean


def load_domains(dataset: str, task_ids: set[str]) -> dict[str, list[str]]:
    records = json.loads(Path(dataset).read_text(encoding="utf-8"))
    return {r["id"]: [s.get("domain", "unknown") for s in r.get("subtasks", [])]
            for r in records if r.get("id") in task_ids}


def summarize(path: Path, users: set[str], domains: dict[str, list[str]]) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    rewards, per_domain, terminations, tool_errors = {}, {}, {}, {}
    trials = set()
    for simulation in data["simulations"]:
        task_id = simulation["task_id"]
        if task_id not in users:
            continue
        trials.add(simulation.get("trial", 0))
        info = simulation["reward_info"]
        rewards.setdefault(task_id, []).append(info["reward"])
        detail = (info.get("info") or {}).get("subtask_rewards") or {}
        for i in range(len(domains.get(task_id, []))):
            key = f"subtask_{i}_reward"
            if key in detail:
                per_domain.setdefault(domains[task_id][i], []).append(detail[key])
        terminations.setdefault(task_id, []).append(simulation["termination_reason"])
        tool_errors.setdefault(task_id, []).append(
            sum(1 for message in simulation.get("messages", [])
                if message.get("role") == "tool" and message.get("error")))
    return {"per_user": {task: mean(values) for task, values in rewards.items()},
            "trials_per_user": len(trials),
            "domains": {name: {"n": len(values), "mean": mean(values)}
                        for name, values in per_domain.items()},
            "terminations": terminations, "tool_errors": tool_errors}


def paired(users: list[str], baseline: dict, candidate: dict, samples: int, seed: int) -> dict:
    rng = random.Random(seed)
    diffs = []
    for _ in range(samples):
        picked = [rng.choice(users) for _ in users]
        diffs.append(mean(candidate[u] - baseline[u] for u in picked))
    diffs.sort()
    return {"delta": mean(candidate[u] - baseline[u] for u in users),
            "ci95": [diffs[int(0.025 * samples)], diffs[int(0.975 * samples) - 1]],
            "p_delta_gt_0": sum(d > 0 for d in diffs) / samples,
            "p_delta_lt_0": sum(d < 0 for d in diffs) / samples}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--samples", type=int, default=10000)
    args = parser.parse_args()

    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    users = list(plan["selected_user_ids"])
    domains = load_domains(args.dataset, set(users))
    baseline = summarize(Path(args.baseline), set(users), domains)
    arms = {"baseline": baseline}
    for run in plan["runs"]:
        arms[run["variant"]] = summarize(Path(run["result"]), set(users), domains)
    missing = [u for u in users if u not in baseline["per_user"]]
    if missing:
        raise SystemExit(f"baseline is missing users: {missing}")

    summary = {"users": users, "trials_per_user": baseline["trials_per_user"],
               "variants": {name: {"mean": mean(arm["per_user"][u] for u in users),
                                   **arm} for name, arm in arms.items()},
               "paired": {}}
    for name, arm in arms.items():
        if name == "baseline":
            continue
        summary["paired"][name] = paired(users, baseline["per_user"], arm["per_user"],
                                         args.samples, plan["seed"])
    out = Path("reports/2026-10-27-go-dev-plain/analysis.json")
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=float) + "\n",
                   encoding="utf-8")

    print("| arm | mean | " + " | ".join(users) + " |")
    print("| --- | ---: | " + " | ".join("---:" for _ in users) + " |")
    for name in arms:
        arm = arms[name]
        print(f"| {name} | {mean(arm['per_user'][u] for u in users):.3f} | "
              + " | ".join(f"{arm['per_user'][u]:.2f}" for u in users) + " |")
    for name, stats in summary["paired"].items():
        lo, hi = stats["ci95"]
        print(f"paired {name} - baseline: {stats['delta']:+.3f} (95% CI {lo:+.3f}..{hi:+.3f})")
    print(json.dumps({name: arm["domains"] for name, arm in arms.items()},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
