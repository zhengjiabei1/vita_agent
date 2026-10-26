"""Export only observed tool failures from real TRAIN simulations.

A low rubric score alone never implies a failure cause. Unknown errors and
unsuccessful runs without an explicit tool error are counted and skipped. Output
omits user text, tool arguments and raw error messages; it is suitable as input
to SkillBank's conservative rule-template distiller.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re


def _failure_code(content: str) -> str | None:
    patterns = (
        (r"\b(?:not found|does not exist|unknown (?:product|hotel|order|flight|train))\b", "unknown_entity"),
        (r"\b(?:out of stock|insufficient (?:stock|quantity)|not enough (?:stock|quantity)|sold out)\b", "unavailable"),
        (r"\b(?:quantity|count)\b.*\b(?:positive|greater than zero|invalid)\b", "invalid_quantity"),
        (r"\b(?:check.?out|end date)\b.*\b(?:before|earlier than)\b", "invalid_dates"),
    )
    for pattern, code in patterns:
        if re.search(pattern, content, re.I):
            return code
    return None


def _domain(tool: str) -> str:
    if "delivery" in tool:
        return "delivery"
    if "instore" in tool or "reservation" in tool:
        return "instore"
    if any(word in tool for word in ("hotel", "flight", "train", "taxi", "attraction", "ota")):
        return "ota"
    return "general"


def export_failed_traces(result_path: str | Path, plan_path: str | Path, output: str | Path) -> dict:
    result_path, plan_path, output = Path(result_path).resolve(), Path(plan_path).resolve(), Path(output).resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("partition") != "train":
        raise ValueError("Trace export requires a predeclared training experiment")
    if str(result_path) not in {run["result"] for run in plan["runs"]}:
        raise ValueError("Result file was not declared by the training plan")
    training = set(plan["dataset"]["tasks"]["train"])
    selected = set(plan["selected_task_ids"])
    if not selected or not selected <= training:
        raise ValueError("Selected tasks are not a valid training cohort")
    results = json.loads(result_path.read_text(encoding="utf-8"))
    simulations = results.get("simulations")
    if not isinstance(simulations, list):
        raise ValueError("Expected official simulation records")
    source_sha = hashlib.sha256(result_path.read_bytes()).hexdigest()
    traces = []
    unknown_errors = unscored = unsuccessful_without_errors = successful = 0
    for number, simulation in enumerate(simulations):
        task_id = simulation.get("task_id")
        if task_id not in selected:
            raise ValueError("Simulation task is outside the predeclared training cohort")
        score = (simulation.get("reward_info") or {}).get("reward")
        if type(score) not in (int, float) or not 0 <= score <= 1:
            unscored += 1
            continue
        if score == 1:
            successful += 1
            continue
        steps = []
        domains = set()
        for index, message in enumerate(simulation.get("messages", [])):
            if not isinstance(message, dict) or message.get("role") != "tool" or message.get("error") is not True:
                continue
            tool, content = message.get("name"), message.get("content")
            if not isinstance(tool, str) or not isinstance(content, str):
                unknown_errors += 1
                continue
            code = _failure_code(content)
            if code is None:
                unknown_errors += 1
                continue
            domains.add(_domain(tool))
            steps.append({"tool": tool, "violations": [{"code": code}], "source_message_index": index})
        if not steps:
            unsuccessful_without_errors += 1
            continue
        trace_identity = f"{source_sha}:{number}:{task_id}:{simulation.get('trial')}"
        traces.append({"trace_id": "real-" + hashlib.sha256(trace_identity.encode()).hexdigest(),
                       "task_id": task_id, "split": "train", "success": False,
                       "domain": next(iter(domains)) if len(domains) == 1 else "general", "steps": steps,
                       "provenance": {"result_sha256": source_sha, "simulation_index": number,
                                      "dataset_sha256": plan["dataset"]["sha256"]},
                       "interpretation": "Observed tool errors in an unsuccessful training run; not causal attribution of its score"})
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        for trace in traces:
            stream.write(json.dumps(trace, ensure_ascii=False) + "\n")
    return {"output": str(output), "source_sha256": source_sha, "exported": len(traces),
            "successful_skipped": successful, "unknown_tool_errors_skipped": unknown_errors,
            "unscored_skipped": unscored, "unsuccessful_without_known_tool_errors": unsuccessful_without_errors}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = export_failed_traces(args.results, args.plan, args.output)
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(2, f"error: {exc}\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
