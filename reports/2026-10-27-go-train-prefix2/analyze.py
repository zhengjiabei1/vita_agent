"""Summarize the real OpenCode Go training-prefix runs without raw user text.

Reads the local result JSON files (not published) and the train prefix2 dataset,
then writes a de-identified summary: hashed task id, rewards, termination, tool
counts, token usage and coarse constraint outcomes. It does not copy prompts,
addresses, names, order ids or model text.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RESULTS = {
    "baseline": ROOT / "artifacts/go-train-prefix2-v1/baseline.json",
    "harness": ROOT / "artifacts/go-train-prefix2-v1/harness.json",
    "memory": ROOT / "artifacts/go-train-prefix2-memory-v1/memory.json",
}
DEFAULT_DATASET = Path("/private/var/folders/d4/vd4q8jqx4nldqbb631tgpw7h0000gn/T/opencode/prefix2/personalization/tasks.json")
MEAT_TAGS = {"牛肉", "猪肉", "鸡肉", "羊肉", "鸭肉"}


def h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def message_usage(messages):
    prompt = completion = 0
    for message in messages:
        if message.get("role") != "assistant":
            continue
        usage = message.get("usage") or {}
        prompt += usage.get("prompt_tokens", 0) or 0
        completion += usage.get("completion_tokens", 0) or 0
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}


def hotel_metadata(dataset_task, hotel_id):
    for subtask in dataset_task["subtasks"][:2]:
        for key, hotel in (subtask.get("environment", {}).get("hotels") or {}).items():
            if key == hotel_id or hotel.get("hotel_id") == hotel_id:
                return hotel.get("hotel_name", ""), set(hotel.get("tags", []))
    return "", set()


def analyze_arm(name: str, path: Path, dataset_task):
    data = json.loads(path.read_text())
    simulation = data["simulations"][0]
    messages = simulation["messages"]
    states = (simulation.get("states") or {}).get("new_states", [])
    delivery = next((state for state in states if state.get("order_type") == "delivery"), None)
    hotels = [state for state in states if state.get("order_type") == "hotel"]
    hotel = hotels[0] if hotels else None
    product_tags = {tag for product in (delivery or {}).get("products", []) for tag in product.get("tags", [])}
    hotel_name, hotel_tags = hotel_metadata(dataset_task, (hotel or {}).get("store_id"))
    hotel_dates = sorted({product.get("date") for state in hotels
                          for product in state.get("products", []) if product.get("date")})
    hotel_unpaid = any(state.get("status") != "paid" for state in hotels)
    risks = []
    if delivery is None:
        risks.append("delivery_order_missing")
    else:
        if not product_tags & MEAT_TAGS:
            risks.append("delivery_no_accepted_meat")
        if product_tags & {"高GI", "加工食物", "油炸"}:
            risks.append("delivery_high_gi_or_processed")
        if delivery.get("status") != "paid":
            risks.append("delivery_unpaid")
    if hotel is None:
        risks.append("hotel_order_missing")
    else:
        if "喜来登" not in hotel_name:
            risks.append("hotel_brand_not_sheraton")
        if "电话叫醒" not in hotel_tags:
            risks.append("hotel_no_phone_wakeup")
        if hotel_unpaid:
            risks.append("hotel_unpaid")
        if hotel_dates != ["2026-04-03", "2026-04-04"]:
            risks.append("hotel_dates_incomplete")
    return {
        "arm": name,
        "task_hash": h(simulation["task_id"]),
        "reward": (simulation.get("reward_info") or {}).get("reward"),
        "subtask_rewards": ((simulation.get("reward_info") or {}).get("info") or {}).get("subtask_rewards"),
        "update_subset_rewards": (((simulation.get("reward_info") or {}).get("info") or {})
                                  .get("subtask_rewards", {}).get("subtask_1_reward")),
        "termination_reason": simulation.get("termination_reason"),
        "duration_seconds": round(simulation.get("duration") or 0, 2),
        "message_count": len(messages),
        "tool_call_count": sum(len(message.get("tool_calls") or []) for message in messages),
        "tool_errors": sum(1 for message in messages if message.get("role") == "tool" and message.get("error")),
        "guard_or_fallback_blocks": sum(
            1 for message in messages if message.get("role") == "assistant"
            and ("尚未执行" in (message.get("content") or "") or "系统校验" in (message.get("content") or ""))),
        "message_usage": message_usage(messages),
        "orders": {
            "delivery": None if delivery is None else {
                "status": delivery.get("status"), "total_price": delivery.get("total_price"),
                "product_tags": sorted(product_tags)},
            "hotel": None if hotel is None else {
                "orders": len(hotels), "all_paid": not hotel_unpaid,
                "brand_is_sheraton": "喜来登" in hotel_name,
                "has_phone_wakeup": "电话叫醒" in hotel_tags,
                "dates": hotel_dates},
        },
        "observed_risks": risks,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("summary.json"))
    args = parser.parse_args()
    dataset = json.loads(args.dataset.read_text())
    baseline = json.loads(DEFAULT_RESULTS["baseline"].read_text())
    task_id = baseline["simulations"][0]["task_id"]
    dataset_task = next(task for task in dataset if task["id"] == task_id)
    arms = [analyze_arm(name, path, dataset_task) for name, path in DEFAULT_RESULTS.items()]
    result = {
        "schema_version": 2,
        "recorded_date": "2026-10-27",
        "status": "real_training_prefix_only",
        "provider": "OpenCode Go",
        "model": "deepseek-v4.1-flash",
        "endpoint_host": "opencode.ai",
        "scope": "1 training user x first 2 subtasks x 1 trial per arm; NOT a held-out test result",
        "task_hash": h(task_id),
        "dataset_sha256": "c76985f042b0be3f751e7a97261e3fe70f6e96402bc6de7f358e025433c523fe",
        "seed": 42,
        "max_steps": 20,
        "arms": arms,
        "shared_ledger": {
            "requests": 176, "request_limit": 200, "remaining_requests": 24,
            "prompt_tokens": 1828931, "completion_tokens": 56745, "total_tokens": 1885676,
            "measured_usd_estimate": 0.30838665, "currency": "USD",
            "pricing_basis": "configured_token_rates",
            "note": "Token usage valued at configured OpenCode model-list prices; not a provider invoice. Provider list_cost/cache discounts may differ.",
        },
        "comparisons": {
            "baseline_vs_harness_delta": 0.0,
            "baseline_vs_memory_delta": 0.0,
            "paired_user_confidence_interval": None,
            "reason_no_ci": "Only one user; no user-level bootstrap interval.",
        },
        "conclusion": {
            "performance_improvement_established": False,
            "all_three_arms_scored_zero": True,
            "evidence": [
                "Baseline and harness both ended at MAX_STEPS with 0.0 subtask rewards.",
                "Baseline and harness selected a high-GI delivery product and a non-Sheraton hotel brand.",
                "Memory selected a Sheraton-brand hotel, but the room lacked the required phone-wakeup service and the delivery product was fish rather than the rubric's accepted meat.",
                "No tool errors appeared in any arm; failures are preference/constraint adherence, not tool crashes.",
            ],
            "not_established": [
                "No module benefit or causal effect.",
                "No held-out test score.",
                "No multi-trial stability estimate.",
                "No skills/full result because no validated frozen skill bank or remaining quota.",
            ],
        },
    }
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "arms": [
        {"arm": arm["arm"], "reward": arm["reward"], "termination": arm["termination_reason"],
         "risks": arm["observed_risks"], "message_usage": arm["message_usage"]} for arm in arms]},
        ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
