"""Local demos, offline skill distillation and paired experiment reporting."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile

from .harness import GuardedHarness
from .memory import PreferenceMemory
from .models import Action, Evidence, Proposal
from .sandbox import CatalogSandbox, example_catalog
from .skills import FrozenSkillBank, SkillBank


def _write_json(path: Path, value: object):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def demo(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("Demo output directory must be empty; existing reports and frozen banks are preserved")
    frozen_path = output / "skills.frozen.json"
    with PreferenceMemory() as memory, SkillBank() as bank:
        memory.remember(Evidence("budget-before", "demo-user", "My meal budget is at most 60 yuan.", 1),
                        domain="delivery", slot="total_price", value=60, strength="hard", operator="lte")
        memory.remember(Evidence("budget-update", "demo-user", "From now on my meal budget is at most 30 yuan.", 2),
                        domain="delivery", slot="total_price", value=30, strength="hard", operator="lte")
        memory.remember(Evidence("allergy", "demo-user", "I am allergic to peanuts.", 3),
                        domain="delivery", slot="allergens", value=["peanut"], strength="hard", operator="excludes")
        memory.remember(Evidence("taste", "demo-user", "I prefer Sichuan cuisine.", 4),
                        domain="delivery", slot="cuisine", value="sichuan", strength="soft")
        for index, (domain, tool, code) in enumerate([
            ("delivery", "order_food", "budget_exceeded"),
            ("delivery", "order_food", "preference_conflict"),
            ("ota", "book_hotel", "invalid_dates"),
        ]):
            bank.distill({"trace_id": f"fixture-trace-{index}", "task_id": f"train-user-{index}",
                          "split": "train", "success": False, "domain": domain,
                          "steps": [{"tool": tool, "violations": [{"code": code}]}]})
        # Scripted utility outcomes demonstrate mechanics, not measured gains.
        low_utility = bank.list_skills()[0]
        for _ in range(3):
            bank.record_outcome(low_utility.id, success=False, split="dev")
        retired = bank.retire(min_trials=3, min_utility=0)
        digest = bank.freeze(frozen_path, excluded_task_ids=["demo-user"])
        frozen = FrozenSkillBank.load(frozen_path)
        sandbox = CatalogSandbox(example_catalog())
        harness = GuardedHarness(memory, sandbox.resolve, sandbox.execute, skill_bank=frozen)
        choices = iter(["premium-salad", "peanut-noodles", "rice-bowl"])
        def planner(context, feedback):
            # Stand-in for LLM drafts: deterministic and zero paid usage.
            return Proposal(Action("order_food", {"product_id": next(choices), "quantity": 1}, "demo-order"))
        lunch = harness.run("demo-user", "delivery", "Order lunch within my latest budget and allergy constraints", planner)
        hotel_choices = iter([("2026-11-03", "2026-11-01"), ("2026-11-01", "2026-11-03")])
        def hotel_planner(context, feedback):
            start, end = next(hotel_choices)
            return Proposal(Action("book_hotel", {"hotel_id": "hotel-central", "check_in": start,
                                                   "check_out": end, "guests": 1}, "demo-hotel"))
        hotel = harness.run("demo-user", "ota", "Book a two-night hotel stay", hotel_planner)
        report = {
            "data_source": "local synthetic fixtures with scripted planner; NOT VitaBench scores",
            "training": "none; no model weights updated",
            "lunch": lunch.to_dict(), "hotel": hotel.to_dict(),
            "executed_operations": len(sandbox.executions),
            "preference_history": [p.to_dict() for p in memory.history("demo-user", "total_price")],
            "memory_context": memory.render("demo-user", "lunch", domain="delivery"),
            "skills": {"frozen_sha256": digest, "active": len(frozen.list_skills()), "retired": retired,
                       "utility_outcomes": "scripted demonstration only"},
            "benchmark_results": None,
        }
        _write_json(output / "report.json", report)
        harness.journal.close()
        return {"output": str(output.resolve()), "lunch_status": lunch.status,
                "lunch_drafts": len(lunch.attempts), "hotel_status": hotel.status,
                "hotel_drafts": len(hotel.attempts), "executed_operations": len(sandbox.executions),
                "frozen_skills": len(frozen.list_skills()), "benchmark_results": None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    demo_parser = sub.add_parser("demo", help="Run offline service scenarios with a scripted planner")
    demo_parser.add_argument("--output", type=Path, help="Fresh directory; defaults to a unique artifacts/demo-* directory")
    distill = sub.add_parser("distill", help="Distill failed training traces and freeze a skill bank")
    distill.add_argument("traces", type=Path)
    distill.add_argument("--output", type=Path, required=True)
    distill.add_argument("--heldout", type=Path, help="JSON list of forbidden task/user IDs")
    compare = sub.add_parser("compare", help="Compare paired official VitaBench result files")
    compare.add_argument("baseline", type=Path)
    compare.add_argument("candidate", type=Path)
    compare.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            if args.output is None:
                Path("artifacts").mkdir(exist_ok=True)
                args.output = Path(tempfile.mkdtemp(prefix="demo-", dir="artifacts"))
            result = demo(args.output)
        elif args.command == "distill":
            excluded = json.loads(args.heldout.read_text(encoding="utf-8")) if args.heldout else []
            if not isinstance(excluded, list) or any(not isinstance(v, str) for v in excluded):
                raise ValueError("heldout must be a JSON list of task/user ID strings")
            with SkillBank() as bank:
                for number, line in enumerate(args.traces.read_text(encoding="utf-8").splitlines(), 1):
                    if not line.strip():
                        continue
                    try:
                        bank.distill(json.loads(line))
                    except (ValueError, KeyError, TypeError) as exc:
                        raise ValueError(f"Trace line {number}: {exc}") from exc
                digest = bank.freeze(args.output, excluded_task_ids=excluded)
                result = {"output": str(args.output), "sha256": digest, "skills": len(bank.list_skills())}
        else:
            from .evaluation import compare_runs
            result = compare_runs(args.baseline, args.candidate)
            if args.output:
                _write_json(args.output, result)
    except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
        parser.exit(2, f"error: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
