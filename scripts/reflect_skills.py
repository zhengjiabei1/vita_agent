"""Run citation-checked LLM reflection on a declared TRAIN result.

Every model request goes through the shared budget/quota guard. Partial
candidates are never frozen: if a request fails or the provider reports quota
exhaustion, the script stops and writes a diagnostic report only.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

from vita_agent.experiments import verify_plan
from vita_agent.skill_reflection import freeze_reflected_bank, reflect_training_results


def _reflection_generator(model: str):
    from vita.data_model.message import SystemMessage, UserMessage
    from vita.utils.llm_utils import generate

    def invoke(payload):
        response = generate(
            model=model,
            messages=[SystemMessage(role="system", content=payload["system"]),
                      UserMessage(role="user", content=json.dumps(payload, ensure_ascii=False))],
        )
        text = response.content or ""
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            try:
                from json_repair import loads as repair_loads
                data = repair_loads(text)
            except Exception:
                data = {}
        return {"skills": data.get("skills") if isinstance(data, dict) else None,
                "cost": response.cost, "usage": response.usage}
    return invoke


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--model-config", required=True, type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", required=True, type=Path, help="New v2 frozen snapshot path")
    parser.add_argument("--report", type=Path, help="Diagnostic report path; defaults beside output")
    quotas = parser.add_mutually_exclusive_group(required=True)
    quotas.add_argument("--max-requests", type=int)
    quotas.add_argument("--max-credits", type=float)
    quotas.add_argument("--budget-cny", type=float)
    quotas.add_argument("--budget-usd", type=float)
    parser.add_argument("--budget-ledger", type=Path, required=True,
                        help="Shared quota/cost ledger also used by experiment executions")
    args = parser.parse_args(argv)

    plan_path = args.plan.resolve()
    plan = verify_plan(plan_path, require_credentials=True)
    if plan.get("partition") != "train":
        parser.exit(2, "error: reflection requires a predeclared train plan\n")
    if args.output.exists():
        parser.exit(2, f"error: refusing to overwrite {args.output}\n")
    report_path = (args.report or args.output.with_suffix(".report.json")).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)

    # Upstream reads model configuration during import.
    os.environ["VITA_MODEL_CONFIG_PATH"] = plan["model"]["path"]
    from vita_agent.budget import BudgetExceeded, UsageUnavailable, install_budget_guard
    from vita_agent.skills import FrozenSkillBank

    billing = plan["model"]["billing"]
    if billing == "free":
        unit = plan["model"]["free_quota"]["unit"]
        if unit == "request":
            if args.max_requests is None:
                parser.exit(2, "error: free request quota requires --max-requests\n")
            mode, unit_name, limit, currency = "requests", "request", args.max_requests, "FREE"
        else:
            if args.max_credits is None:
                parser.exit(2, "error: free credit quota requires --max-credits\n")
            mode, unit_name, limit, currency = "credits", "credit", args.max_credits, "FREE"
        pricing_basis = "free_provider_quota"
    else:
        if args.budget_cny is None and args.budget_usd is None:
            parser.exit(2, "error: paid model requires --budget-cny or --budget-usd\n")
        currency = "CNY" if args.budget_cny is not None else "USD"
        if plan["model"]["currency"] != currency:
            parser.exit(2, "error: budget currency does not match model tariff\n")
        limit = args.budget_cny if args.budget_cny is not None else args.budget_usd
        mode, unit_name, pricing_basis = "currency", "token_cost", plan["model"]["pricing_basis"]

    ledger = install_budget_guard(args.budget_ledger, limit=limit, currency=currency,
                                  pricing_basis=pricing_basis, mode=mode, unit=unit_name)
    result = {"schema_version": 1, "status": "started", "model": plan["model"]["model"],
              "plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(), "billing": billing,
              "quota_mode": mode, "quota_unit": unit_name, "quota_limit": limit,
              "currency": currency, "started_at": datetime.now(timezone.utc).isoformat(),
              "excluded_task_ids": sorted(set(plan["dataset"]["tasks"]["dev"])
                                          | set(plan["dataset"]["tasks"]["test"]))}
    try:
        reflection = reflect_training_results(args.results, plan_path, _reflection_generator(args.model))
    except (BudgetExceeded, UsageUnavailable) as exc:
        result.update(status="stopped_quota_or_usage", error=type(exc).__name__, message=str(exc))
        result["ledger"] = ledger.summary()
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": result["status"], "report": str(report_path)}, ensure_ascii=False))
        raise SystemExit(2)
    except Exception as exc:
        # Provider errors (including an uncaught 429) must leave a report and
        # must never freeze partially validated candidates.
        result.update(status="stopped_provider_or_validation_error",
                      error=type(exc).__name__, message=str(exc)[:500])
        result["ledger"] = ledger.summary()
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": result["status"], "report": str(report_path)}, ensure_ascii=False))
        raise SystemExit(2)

    result.update(status="reflected", candidates=len(reflection.candidates),
                  diagnostics=reflection.diagnostics, costs=reflection.costs,
                  usage=reflection.usage, ledger=ledger.summary())
    if reflection.candidates:
        digest = freeze_reflected_bank(args.output, reflection.candidates,
                                       excluded_task_ids=result["excluded_task_ids"])
        FrozenSkillBank.load(args.output)  # Verify the dispatched v2 loader end to end.
        result.update(status="frozen", output=str(args.output), sha256=digest,
                      skills=len(FrozenSkillBank.load(args.output).list_skills()))
    else:
        result.update(status="no_grounded_candidates", output=None,
                      note="No v2 snapshot was written because reflection produced no validated lesson.")
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("status", "candidates", "output", "report")}
                     | {"report": str(report_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
