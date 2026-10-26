"""Reproduce the archived zero-score diagnosis without copying personal text.

Reads the published baseline/harness training snapshots and the train-only
dataset rows already named by those snapshots. Output contains hashed IDs,
counts and coarse choice categories, not addresses, names or raw prompts.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPORTS = ROOT / "reports" / "2026-10-26-train-prefix2"
DATASET = Path("/Users/candice/Desktop/项目/vitabench2-reference/data/vita/domains/personalization/tasks.json")


def h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def usage(messages):
    prompt = completion = 0
    for message in messages:
        item = message.get("usage") or {}
        prompt += item.get("prompt_tokens", 0) or 0
        completion += item.get("completion_tokens", 0) or 0
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion}


def analyze_arm(path: Path, task_id: str):
    data = json.loads(path.read_text())
    simulation = data["simulations"][0]
    messages = simulation["messages"]
    states = (simulation.get("states") or {}).get("new_states", [])
    orders = [state for state in states if state.get("order_type") in ("delivery", "hotel")]
    delivery = next((state for state in orders if state.get("order_type") == "delivery"), None)
    hotel = next((state for state in orders if state.get("order_type") == "hotel"), None)
    blocks = sum(1 for message in messages if message.get("role") == "assistant"
                 and ("尚未执行" in (message.get("content") or "")
                      or "系统校验" in (message.get("content") or "")))
    tool_calls = [call.get("name") for message in messages for call in (message.get("tool_calls") or [])]
    tool_errors = sum(1 for message in messages if message.get("role") == "tool" and message.get("error"))
    product_tags = [tag for product in (delivery or {}).get("products", []) for tag in product.get("tags", [])]
    return {
        "arm_file": path.name,
        "task_hash": h(task_id),
        "trial": simulation.get("trial"),
        "reward": (simulation.get("reward_info") or {}).get("reward"),
        "subtask_rewards": ((simulation.get("reward_info") or {}).get("info") or {}).get("subtask_rewards"),
        "termination_reason": simulation.get("termination_reason"),
        "message_count": len(messages),
        "tool_call_count": len(tool_calls),
        "tool_errors": tool_errors,
        "guard_or_fallback_blocks": blocks,
        "tool_sequence": tool_calls,
        "usage_from_messages": usage(messages),
        "orders_in_final_state": {
            "delivery": None if delivery is None else {
                "total_price": delivery.get("total_price"), "status": delivery.get("status"),
                "product_tags": sorted(set(product_tags))},
            "hotel": None if hotel is None else {
                "status": hotel.get("status"), "hotel_id": hotel.get("store_id"),
                "room_dates": [product.get("date") for product in hotel.get("products", [])]},
        },
        "observed_rubric_risks": [
            code for code, hit in (
                ("delivery_order_missing", delivery is None),
                ("delivery_high_gi_or_processed", any(tag in {"高GI", "加工食物", "油炸"} for tag in product_tags)),
                ("hotel_order_missing", hotel is None),
                ("hotel_unpaid", bool(hotel) and hotel.get("status") != "paid"),
            ) if hit
        ],
    }


def hotel_brand(dataset_task, hotel_id):
    for subtask in dataset_task["subtasks"][:2]:
        for key, hotel in (subtask.get("environment", {}).get("hotels") or {}).items():
            if key == hotel_id or hotel.get("hotel_id") == hotel_id:
                return hotel.get("hotel_name", "")
    return ""


def main():
    baseline = json.loads((REPORTS / "baseline.json").read_text())
    task_id = baseline["simulations"][0]["task_id"]
    dataset_task = next(task for task in json.loads(DATASET.read_text()) if task["id"] == task_id)
    arms = [analyze_arm(REPORTS / name, task_id) for name in ("baseline.json", "harness.json")]
    for arm in arms:
        hotel = arm["orders_in_final_state"]["hotel"]
        if hotel:
            name = hotel_brand(dataset_task, hotel["hotel_id"])
            arm["orders_in_final_state"]["hotel"]["brand_is_sheraton"] = "喜来登" in name
            arm["orders_in_final_state"]["hotel"]["brand_label"] = (
                "sheraton" if "喜来登" in name else "other_brand")
            if "喜来登" not in name:
                arm["observed_rubric_risks"].append("hotel_brand_not_sheraton")
    result = {
        "schema_version": 1,
        "source": "reports/2026-10-26-train-prefix2/{baseline,harness}.json",
        "scope": "One training user, first two subtasks, one trial per arm. NOT a test-set score.",
        "dataset_sha256": hashlib.sha256(DATASET.read_bytes()).hexdigest(),
        "task_hash": h(task_id),
        "subtask_domains": [subtask["domain"] for subtask in dataset_task["subtasks"][:2]],
        "arms": arms,
        "conclusion": {
            "score_is_evaluator_bug": False,
            "evidence": [
                "Both arms ended with user_stop, not a crash or missing evaluation.",
                "Baseline created both orders but selected a high-GI delivery product and a non-Sheraton hotel brand.",
                "Harness blocked the delivery proposal repeatedly; its final state contains no delivery order, and its hotel order was unpaid.",
                "The observed choices contradict the training user's explicit historical brand preference and the delivery rubric's health constraints.",
            ],
            "interpretation": "The 0.0 scores are consistent with preference/constraint adherence failures and guard blocking, not evidence of an evaluator defect or a measured module benefit.",
            "not_established": [
                "No causal effect of guards, memory or skills.",
                "No test-set performance.",
                "No claim that more samples alone would change the result.",
            ],
        },
    }
    output = Path(__file__).with_name("diagnosis.json")
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "arms": [
        {"file": arm["arm_file"], "reward": arm["reward"], "risks": arm["observed_rubric_risks"],
         "blocks": arm["guard_or_fallback_blocks"]} for arm in arms]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
