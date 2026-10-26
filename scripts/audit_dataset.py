"""Measure data/preparation coverage without models or evaluation labels."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from vita_agent.evaluation import split_users
from vita_agent.interactions import interaction_evidence
from vita_agent.memory import extract_preferences


def audit(dataset: Path, seed=42):
    raw = dataset.read_bytes()
    tasks = json.loads(raw)
    split = split_users([t["id"] for t in tasks], seed=seed)
    counts = {}
    for name, ids in split.items():
        if not isinstance(ids, list):
            continue
        selected = [t for t in tasks if t["id"] in ids]
        counts[name] = {"users": len(selected), "subtasks": sum(len(t["subtasks"]) for t in selected),
                       "domains": dict(Counter(s["domain"] for t in selected for s in t["subtasks"]))}
    evidence = {}
    records = parse_errors = 0
    # Development diagnostics use train only; don't inspect holdout preference content.
    for task in tasks:
        if task["id"] not in split["train"]:
            continue
        for subtask in task["subtasks"]:
            for record in subtask["interactions"]:
                records += 1
                try:
                    for item, domain in interaction_evidence(record, task["user_id"]):
                        evidence[item.id] = (item, domain)
                except (ValueError, TypeError, OverflowError):
                    parse_errors += 1
    slots = Counter()
    extracted_messages = 0
    for item, domain in evidence.values():
        extracted = extract_preferences(item, domain)
        extracted_messages += bool(extracted)
        slots.update(p["slot"] for p in extracted)
    return {
        "dataset_sha256": hashlib.sha256(raw).hexdigest(), "dataset_bytes": len(raw),
        "users": len(tasks), "subtasks": sum(len(t["subtasks"]) for t in tasks),
        "seed": seed, "split": counts,
        "train_preparation": {"interaction_records": records, "parse_errors": parse_errors,
            "unique_user_utterances": len(evidence), "rule_extracted_utterances": extracted_messages,
            "rule_extraction_fraction": extracted_messages / len(evidence) if evidence else None,
            "extracted_slots": dict(slots)},
        "interpretation": "Measured parsing/extraction coverage on train; NOT preference recall, task success, or model performance. No gold labels read.",
        "benchmark_scores": None,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.dataset)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False, indent=2))
