"""Reproducible, fail-closed orchestration of real VitaBench ablations.

Planning never calls a model. Executing starts the actual upstream orchestrator;
there is no scripted planner or fabricated benchmark score in this module.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit

from .evaluation import compare_runs, split_users
from .skills import FrozenSkillBank

UPSTREAM_COMMIT = "f60169e89f30499cb7883f3dad76bd03facc908d"
VARIANTS = {
    "baseline": {"memory": "full_context", "guards": False, "skills": False},
    "harness": {"memory": "full_context", "guards": True, "skills": False},
    "memory": {"memory": "structured", "guards": False, "skills": False},
    "skills": {"memory": "full_context", "guards": False, "skills": True},
    "full": {"memory": "structured", "guards": True, "skills": True},
}
_ENV = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_new(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def dataset_manifest(path: str | Path, *, seed: int = 42) -> dict:
    """Validate metadata and split by user, keeping every task for a user together."""
    path = Path(path).resolve()
    records = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(records, list) or not records:
        raise ValueError("Official dataset must be a nonempty task list")
    users: dict[str, list[str]] = {}
    counts = {}
    for task in records:
        if not isinstance(task, dict) or not isinstance(task.get("id"), str) or not task["id"]:
            raise ValueError("Each task must have a nonempty string id")
        if task["id"] in counts:
            raise ValueError("Dataset has duplicate task ids")
        user = task.get("user_id")
        if not isinstance(user, str) or not user:
            raise ValueError("Each task needs a real user_id for leakage-safe splitting")
        subtasks = task.get("subtasks")
        if not isinstance(subtasks, list) or not subtasks:
            raise ValueError("Each task needs a nonempty subtask list")
        ids = []
        for subtask in subtasks:
            if not isinstance(subtask, dict) or subtask.get("domain") not in ("delivery", "instore", "ota"):
                raise ValueError("Each subtask must declare an official domain")
            if not isinstance(subtask.get("subtask_id"), str) or not subtask["subtask_id"]:
                raise ValueError("Each subtask needs a subtask_id")
            if not isinstance(subtask.get("instruction"), str) or not isinstance(subtask.get("environment"), dict):
                raise ValueError("Each subtask needs instruction and environment")
            ids.append(subtask["subtask_id"])
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate subtask ids in a task")
        users.setdefault(user, []).append(task["id"])
        counts[task["id"]] = len(subtasks)
    partitions = split_users(users, seed=seed)
    return {"path": str(path), "sha256": _sha(path), "split_seed": seed,
            "user_count": len(users), "task_count": len(records), "subtask_count": sum(counts.values()),
            "users": partitions,
            "tasks": {name: sorted(t for user in values for t in users[user]) for name, values in partitions.items()},
            "user_tasks": {u: sorted(v) for u, v in sorted(users.items())}, "subtasks_per_task": counts}


def model_preflight(path: str | Path, model: str, *, environment=None) -> dict:
    """Return metadata only: no key, bearer token, URL userinfo or raw config."""
    try:
        import yaml  # Required by the official optional integration, not core demos.
    except ModuleNotFoundError as exc:
        raise ValueError("Install the official VitaBench optional dependencies for model preflight") from exc
    path = Path(path).resolve()
    environment = os.environ if environment is None else environment
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("default", {}), dict):
        raise ValueError("Invalid official models YAML")
    models = value.get("models", [])
    if not isinstance(models, list):
        raise ValueError("models must be a list")
    matches = [m for m in models if isinstance(m, dict) and m.get("name") == model]
    if len(matches) != 1:
        raise ValueError("Requested model must be explicitly declared exactly once")
    settings = dict(value.get("default", {}))
    settings.update(matches[0])
    required, missing = set(), set()

    def resolve(v):
        if isinstance(v, str) and (match := _ENV.fullmatch(v)):
            name = match[1]
            required.add(name)
            if not environment.get(name):
                missing.add(name)
            return environment.get(name)
        if isinstance(v, dict):
            return {k: resolve(x) for k, x in v.items()}
        if isinstance(v, list):
            return [resolve(x) for x in v]
        return v

    resolved = resolve(settings)
    endpoint = resolved.get("base_url")
    parsed = urlsplit(endpoint) if isinstance(endpoint, str) else None
    if parsed is None or parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        if not missing:
            raise ValueError("Model needs a valid HTTP(S) base_url without credentials in its URL")
    key_present = bool(resolved.get("api_key"))
    max_tokens = resolved.get("max_tokens")
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
        raise ValueError("Explicit positive max_tokens is required for experimental control")
    billing = resolved.get("billing", "paid")
    if billing not in ("paid", "free"):
        raise ValueError("Model config billing must be paid or free")
    tariff_names = [name for name in ("cost_1m_token_dollar", "cost_1m_token_cny") if name in resolved]
    free_quota = None
    if billing == "free":
        if tariff_names:
            raise ValueError("A free model must not declare a token tariff; quota units are not prices")
        quota = resolved.get("free_quota")
        if not isinstance(quota, dict) or quota.get("unit") not in ("request", "credit"):
            raise ValueError("Free models require free_quota.unit of request or credit")
        unit = quota["unit"]
        credit_per_request = quota.get("credit_per_request")
        if unit == "credit" and (
            isinstance(credit_per_request, bool) or not isinstance(credit_per_request, (int, float))
            or not math.isfinite(credit_per_request) or credit_per_request <= 0
        ):
            raise ValueError("Credit-based free models require a positive free_quota.credit_per_request")
        if unit == "request" and credit_per_request is not None:
            raise ValueError("Request-based free models must not declare credit_per_request")
        free_quota = {"unit": unit,
                      "credit_per_request": float(credit_per_request) if unit == "credit" else None,
                      "note": quota.get("note") if isinstance(quota.get("note"), str) else None}
        currency = None
        prices = None
        pricing_basis = resolved.get("pricing_basis", "free_provider_quota")
    else:
        if len(tariff_names) != 1:
            raise ValueError("Configure exactly one explicit USD or CNY token tariff")
        currency = "CNY" if tariff_names[0] == "cost_1m_token_cny" else "USD"
        prices = resolved[tariff_names[0]]
        if not isinstance(prices, dict) or any(
            isinstance(prices.get(k), bool) or not isinstance(prices.get(k), (int, float))
            or not math.isfinite(prices[k]) or prices[k] <= 0
            for k in ("prompt_price", "completion_price")
        ):
            raise ValueError("Explicit positive prompt/completion token prices are required for budget reservation")
        pricing_basis = resolved.get("pricing_basis", "configured_token_prices")
    # The raw file hash detects changed configuration without exporting contents.
    return {"path": str(path), "sha256": _sha(path), "model": model,
            "endpoint_host": parsed.hostname if parsed else None,
            "required_variable_names": sorted(required), "missing_variable_names": sorted(missing),
            "credential_present": key_present, "max_tokens": max_tokens,
            "billing": billing, "currency": currency, "free_quota": free_quota,
            "pricing_basis": pricing_basis,
            "ready": not missing and key_present}


def _sources(root: Path) -> dict[str, str]:
    files = sorted((root / "src" / "vita_agent").glob("*.py")) + sorted((root / "scripts").glob("*.py"))
    return {str(path.relative_to(root)): _sha(path) for path in files}


def build_plan(dataset: str | Path, output: str | Path, *, model_config: str | Path,
               model: str, upstream: str | Path, phase: str = "smoke", variants=None,
               skill_bank: str | Path | None = None, seed: int = 42, num_trials: int = 1,
               max_steps: int = 50, project_root: str | Path | None = None,
               memory_extraction: str = "llm", user_limit: int | None = None) -> dict:
    """Save a non-overwritable plan; smoke/small use dev, full uses held-out test."""
    if phase not in ("smoke", "small", "full", "train"):
        raise ValueError("phase must be smoke, small, full or train")
    if memory_extraction not in ("rules", "llm"):
        raise ValueError("memory_extraction must be rules or llm")
    if user_limit is not None and (isinstance(user_limit, bool) or not isinstance(user_limit, int) or user_limit < 1):
        raise ValueError("user_limit must be a positive integer")
    if phase == "full" and user_limit is not None:
        raise ValueError("full phase evaluates the entire declared test partition; use dev for limited experiments")
    for name, value in (("num_trials", num_trials), ("max_steps", max_steps)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be positive")
    variants = list(variants or VARIANTS)
    if not variants or len(set(variants)) != len(variants) or any(v not in VARIANTS for v in variants):
        raise ValueError("Choose unique known ablations")
    root = Path(project_root or Path(__file__).resolve().parents[2]).resolve()
    output, upstream = Path(output).resolve(), Path(upstream).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("Experiment output must be a new or empty directory")
    data = dataset_manifest(dataset, seed=seed)
    if Path(data["path"]).name != "tasks.json" or Path(data["path"]).parent.name != "personalization":
        raise ValueError("Official loader requires a personalization/tasks.json dataset path")
    partition = "test" if phase == "full" else "train" if phase == "train" else "dev"
    selected_users = data["users"][partition]
    selected_users = selected_users[:1] if phase == "smoke" else selected_users[:3] if phase == "small" else selected_users
    if phase == "train" and user_limit is None:
        selected_users = selected_users[:3]
    if user_limit is not None:
        selected_users = selected_users[:user_limit]
    selected_tasks = sorted(t for u in selected_users for t in data["user_tasks"][u])
    if not selected_tasks:
        raise ValueError("Selected user partition is empty")
    config = model_preflight(model_config, model)
    revision = subprocess.check_output(["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True).strip()
    if revision != UPSTREAM_COMMIT:
        raise ValueError("Official checkout does not match the reviewed pinned commit")
    if subprocess.check_output(["git", "-C", str(upstream), "diff", "HEAD", "--name-only"], text=True).strip():
        raise ValueError("Official tracked source has local changes; preserve a reviewed fixed implementation")
    frozen = None
    if any(VARIANTS[v]["skills"] for v in variants):
        if not skill_bank:
            raise ValueError("Skill ablations require a real frozen training bank")
        bank_path = Path(skill_bank).resolve()
        bank = FrozenSkillBank.load(bank_path)
        skills = bank.list_skills()
        if not skills:
            raise ValueError("An empty bank cannot measure skill contribution")
        seen = {p.task_id for s in skills for p in s.provenance}
        if not seen <= set(data["tasks"]["train"]):
            raise ValueError("Every skill provenance task must belong to this dataset's training partition")
        if seen & set(selected_tasks):
            raise ValueError("Selected cohort overlaps skill training provenance")
        frozen = {"path": str(bank_path), "sha256": _sha(bank_path), "payload_digest": bank.digest, "skills": len(skills)}
    plans = []
    for variant in variants:
        spec = VARIANTS[variant]
        result = output / f"{variant}.json"
        args = ["run", "--domain", "personalization", "--agent-llm", model, "--user-llm", model,
                "--evaluator-llm", model, "--num-trials", str(num_trials), "--max-steps", str(max_steps),
                "--max-concurrency", "1", "--seed", str(seed), "--language", "chinese",
                "--evaluation-type", "trajectory", "--log-level", "INFO", "--save-to", str(result),
                "--task-ids", *selected_tasks]
        args += (["--memory-class", "vita_agent.vitabench.StructuredPreferenceMemory"] if spec["memory"] == "structured"
                 else ["--memory-type", spec["memory"]])
        plans.append({"variant": variant, "upstream_arguments": args, "result": str(result),
                      "environment": {"VITA_MODEL_CONFIG_PATH": config["path"],
                                      "VITA_ENABLE_GUARDS": "1" if spec["guards"] else "0",
                                      "VITA_MEMORY_EXTRACTION": memory_extraction,
                                      "VITA_SKILL_BANK": frozen["path"] if spec["skills"] else ""}})
    from .upstream_fixes import patch_info
    repairs = {**patch_info(), "enabled": True, "status": "planned for every variant"}
    plan = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
            "project_root": str(root), "upstream": str(upstream), "upstream_commit": revision,
            "dataset": data, "model": config, "phase": phase, "partition": partition,
            "selected_user_ids": selected_users, "selected_task_ids": selected_tasks,
            "num_trials": num_trials, "seed": seed, "max_steps": max_steps, "skill_bank": frozen,
            "memory_extraction": memory_extraction,
            "upstream_repairs": repairs,
            "source_sha256": _sources(root), "runs": plans,
            "interpretation": "smoke/dev results are development diagnostics; only predeclared held-out full runs are test results"}
    output.mkdir(parents=True, exist_ok=True)
    _write_new(output / "plan.json", plan)
    return plan


def verify_plan(path: str | Path, *, require_credentials: bool = False) -> dict:
    path = Path(path).resolve()
    plan = json.loads(path.read_text(encoding="utf-8"))
    if plan.get("schema_version") != 1:
        raise ValueError("Unsupported experiment manifest")
    if _sha(Path(plan["dataset"]["path"])) != plan["dataset"]["sha256"]:
        raise ValueError("Dataset changed since plan creation")
    if _sources(Path(plan["project_root"])) != plan["source_sha256"]:
        raise ValueError("Implementation changed since plan creation; create a new plan")
    revision = subprocess.check_output(["git", "-C", plan["upstream"], "rev-parse", "HEAD"], text=True).strip()
    if revision != plan["upstream_commit"] or subprocess.check_output(
        ["git", "-C", plan["upstream"], "diff", "HEAD", "--name-only"], text=True
    ).strip():
        raise ValueError("Official checkout changed since plan creation")
    config = model_preflight(plan["model"]["path"], plan["model"]["model"])
    if config["sha256"] != plan["model"]["sha256"]:
        raise ValueError("Model config changed since plan creation")
    if require_credentials and not config["ready"]:
        raise ValueError("Model credentials unavailable; configure required variable names securely")
    if plan["skill_bank"]:
        if _sha(Path(plan["skill_bank"]["path"])) != plan["skill_bank"]["sha256"]:
            raise ValueError("Frozen skill bank changed since plan creation")
        FrozenSkillBank.load(plan["skill_bank"]["path"])
    return plan


def execute_plan(path: str | Path, *, budget_usd: float | None = None, budget_cny: float | None = None,
                 max_requests: int | None = None, max_credits: float | None = None,
                 budget_ledger: str | Path | None = None) -> dict:
    """Run real evaluations with a shared pre-call cost or free-quota reservation.

    Paid models reserve token-price bounds. Genuinely free models reserve one
    request (or a configured credit cost) per call; zero token prices are never
    supplied as a workaround. Provider billing/quota remains authoritative.
    """
    if budget_usd is None and budget_cny is None and max_requests is None and max_credits is None:
        raise ValueError("Specify an API budget or a request/credit quota")
    if budget_usd is not None and budget_cny is not None:
        raise ValueError("Specify exactly one API budget currency")
    for value, name in ((budget_usd, "API budget"), (budget_cny, "API budget"),
                        (max_credits, "max_credits")):
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                  or not math.isfinite(value) or value <= 0):
            raise ValueError(f"A positive finite {name} is required")
    if max_requests is not None and (
            isinstance(max_requests, bool) or not isinstance(max_requests, int) or max_requests < 1):
        raise ValueError("max_requests must be a positive integer")
    path = Path(path).resolve()
    plan = verify_plan(path, require_credentials=True)
    billing = plan["model"]["billing"]
    request_limit = None
    if billing == "free":
        if max_requests is not None and max_credits is not None:
            raise ValueError("Specify exactly one free request or credit limit")
        quota = plan["model"]["free_quota"]
        if quota["unit"] == "request":
            if max_requests is None:
                raise ValueError("Free request-based model requires --max-requests")
            budget, currency = max_requests, "FREE"
            mode, unit, pricing_basis = "requests", "request", "free_provider_quota"
        else:
            if max_credits is None:
                raise ValueError("Free credit-based model requires --max-credits")
            budget, currency = max_credits, "FREE"
            mode, unit, pricing_basis = "credits", "credit", "free_provider_quota"
        if budget_usd is not None or budget_cny is not None:
            raise ValueError("Do not supply a cash budget for a free model; use a request/credit limit")
    else:
        if max_credits is not None:
            raise ValueError("Provider credits apply only to free credit-based models")
        if budget_usd is None and budget_cny is None:
            raise ValueError("A paid model requires --budget-usd or --budget-cny")
        budget = budget_cny if budget_cny is not None else budget_usd
        currency = "CNY" if budget_cny is not None else "USD"
        if plan["model"]["currency"] != currency:
            raise ValueError("Budget currency must match the configured model tariff")
        mode, unit, pricing_basis = "currency", "token_cost", plan["model"]["pricing_basis"]
        request_limit = max_requests
    root, output = Path(plan["project_root"]), path.parent
    execution = {"budget": budget, "currency": currency, "mode": mode, "unit": unit,
                 "request_limit": request_limit,
                 "budget_enforcement": ("shared pre-call SQLite request/credit reservation"
                                        if mode != "currency" else
                                        "shared pre-call SQLite reservation; configured prices"),
                 "budget_ledger": str(Path(budget_ledger).resolve() if budget_ledger else output / "budget.sqlite3"),
                 "pricing_basis": pricing_basis,
                 "started_at": datetime.now(timezone.utc).isoformat(), "plan_sha256": _sha(path)}
    for run in plan["runs"]:
        if Path(run["result"]).exists():
            raise ValueError("Result already exists; refuse overwrite or implicit upstream resume")
    _write_new(output / "execution.json", execution)
    completed = []
    for run in plan["runs"]:
        log_path = output / f"{run['variant']}.log"
        with log_path.open("x", encoding="utf-8") as log:
            proc = subprocess.run([sys.executable, str(root / "scripts" / "run_experiments.py"),
                                   "worker", str(path), run["variant"]], cwd=root, stdout=log, stderr=subprocess.STDOUT)
        if proc.returncode:
            _write_new(output / "stopped.json", {"variant": run["variant"], "exit_code": proc.returncode,
                                                 "reason": "upstream_failed", "completed": completed})
            raise RuntimeError("Official evaluation failed; inspect the local log and create a new explicit run")
        # Self-comparison verifies complete output before any later ablation.
        compare_runs(run["result"], run["result"])
        completed.append(run["variant"])
    reports = {}
    runs = {r["variant"]: r for r in plan["runs"]}
    if "baseline" in runs:
        for name, run in runs.items():
            if name != "baseline":
                reports[name] = compare_runs(runs["baseline"]["result"], run["result"])
    _write_new(output / "comparisons.json", reports)
    return {"completed": completed, "comparisons": str(output / "comparisons.json")}


def run_worker(path: str | Path, variant: str) -> None:
    """One official run with all variants using the same custom wrapper."""
    plan = verify_plan(path, require_credentials=True)
    selected = [r for r in plan["runs"] if r["variant"] == variant]
    if len(selected) != 1:
        raise ValueError("Unknown variant in saved plan")
    run = selected[0]
    os.environ.update(run["environment"])
    execution_path = Path(path).resolve().parent / "execution.json"
    if not execution_path.exists():
        raise ValueError("Workers may run only from an explicit budgeted execute invocation")
    execution = json.loads(execution_path.read_text(encoding="utf-8"))
    if execution["plan_sha256"] != _sha(Path(path)):
        raise ValueError("Execution manifest does not match the immutable plan")
    os.environ["VITA_BUDGET_LEDGER"] = execution["budget_ledger"]
    os.environ["VITA_BUDGET_CURRENCY"] = execution["currency"]
    os.environ["VITA_BUDGET_LIMIT"] = str(execution["budget"])
    os.environ["VITA_BUDGET_MODE"] = execution.get("mode", "currency")
    os.environ["VITA_BUDGET_UNIT"] = execution.get("unit", "token_cost")
    # Explicit path ensures the imported upstream source matches the reviewed checkout.
    sys.path.insert(0, str(Path(plan["upstream"]) / "src"))
    from .budget import install_budget_guard
    install_budget_guard(execution["budget_ledger"], limit=execution["budget"], currency=execution["currency"],
                         pricing_basis=execution["pricing_basis"], mode=execution.get("mode", "currency"),
                         unit=execution.get("unit"), request_limit=execution.get("request_limit"))
    os.chdir(plan["upstream"])
    import vita.run as upstream_run
    import vita.domains.personalization.environment as personalization
    from vita.cli import main as vita_main
    from vita.utils.display import ConsoleDisplay
    from .vitabench import make_guarded_agent_class
    from .upstream_fixes import install_upstream_fixes
    repair_info = install_upstream_fixes()
    # Bind only the loader to the original official dataset; no copying gold fields into prompts.
    if Path(plan["dataset"]["path"]).name != "tasks.json":
        raise ValueError("Official loader expects the planned dataset filename tasks.json")
    personalization.DOMAIN_DIR = Path(plan["dataset"]["path"]).parent.parent
    upstream_run.PersonalizationAgent = make_guarded_agent_class()
    install_output_redaction(upstream_run, ConsoleDisplay)
    currency_path = Path(run["result"]).with_suffix(".currency.json")
    _write_new(currency_path, {"currency": execution["currency"], "pricing_basis": execution["pricing_basis"],
                              "cost_semantics": "configured tariff estimate; not a provider invoice"})
    sys.argv = ["vita", *run["upstream_arguments"]]
    vita_main()
    result_path = Path(run["result"])
    if result_path.exists():
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result.setdefault("info", {})["cost_currency"] = execution["currency"]
        result["info"]["cost_basis"] = execution["pricing_basis"]
        result["info"]["upstream_repairs"] = repair_info
        result["info"]["memory_extraction"] = plan["memory_extraction"]
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def redact_settings(value):
    """Remove credentials from configuration metadata, preserving token limits."""
    if isinstance(value, dict):
        cleaned = {}
        for name, item in value.items():
            key = str(name).lower().replace("-", "_")
            if key in {"api_key", "key", "token", "access_token", "refresh_token", "authorization", "password", "secret"} or key.endswith(("_api_key", "_secret", "_password")):
                cleaned[name] = "[REDACTED]"
            elif key in {"base_url", "endpoint", "url"} and isinstance(item, str):
                parsed = urlsplit(item)
                cleaned[name] = f"{parsed.scheme}://{parsed.hostname or ''}{':' + str(parsed.port) if parsed.port else ''}{parsed.path}"
            else:
                cleaned[name] = redact_settings(item)
        return cleaned
    if isinstance(value, list):
        return [redact_settings(item) for item in value]
    return value


def install_output_redaction(upstream_run, console_display) -> None:
    """Sanitize official checkpoint metadata and console config without altering inference."""
    original = getattr(upstream_run.get_info, "_vita_original", upstream_run.get_info)
    def safe_get_info(*args, **kwargs):
        bound = inspect.signature(original).bind_partial(*args, **kwargs)
        for name in tuple(bound.arguments):
            if name.startswith("llm_args"):
                bound.arguments[name] = redact_settings(bound.arguments[name])
        return original(**bound.arguments)
    safe_get_info._vita_original = original
    upstream_run.get_info = safe_get_info
    original_display = getattr(console_display.display_run_config, "_vita_original", console_display.display_run_config)
    def safe_display(config):
        copy = config.model_copy(deep=True)
        for name in ("llm_args_agent", "llm_args_user", "llm_args_evaluator"):
            if hasattr(copy, name):
                setattr(copy, name, redact_settings(getattr(copy, name)))
        return original_display(copy)
    safe_display._vita_original = original_display
    console_display.display_run_config = staticmethod(safe_display)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("plan", help="Create an immutable experiment plan without model calls")
    create.add_argument("--dataset", required=True, type=Path)
    create.add_argument("--output", required=True, type=Path)
    create.add_argument("--model-config", required=True, type=Path)
    create.add_argument("--model", required=True)
    create.add_argument("--upstream", required=True, type=Path)
    create.add_argument("--phase", choices=("smoke", "small", "full", "train"), default="smoke")
    create.add_argument("--variants", nargs="+", choices=tuple(VARIANTS), default=list(VARIANTS))
    create.add_argument("--skill-bank", type=Path)
    create.add_argument("--seed", type=int, default=42)
    create.add_argument("--num-trials", type=int, default=1)
    create.add_argument("--max-steps", type=int, default=50)
    create.add_argument("--memory-extraction", choices=("rules", "llm"), default="llm")
    create.add_argument("--user-limit", type=int)
    execute = sub.add_parser("execute", help="Run real evaluations after prerequisites and quota are configured")
    execute.add_argument("plan", type=Path)
    budgets = execute.add_mutually_exclusive_group()
    budgets.add_argument("--budget-usd", type=float)
    budgets.add_argument("--budget-cny", type=float)
    quotas = execute.add_mutually_exclusive_group()
    quotas.add_argument("--max-requests", type=int,
                        help="Request cap; primary limit for free models, optional secondary cap for paid models")
    quotas.add_argument("--max-credits", type=float, help="Free-tier provider credit quota")
    execute.add_argument("--budget-ledger", type=Path, help="Reuse one ledger across train/dev/test plans to enforce the total project quota")
    check = sub.add_parser("check")
    check.add_argument("plan", type=Path)
    worker = sub.add_parser("worker", help=argparse.SUPPRESS)
    worker.add_argument("plan", type=Path)
    worker.add_argument("variant")
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            values = vars(args).copy()
            values.pop("command")
            result = build_plan(**values)
            result = {"plan": str(args.output / "plan.json"), "users": len(result["selected_user_ids"]),
                      "runs": [r["variant"] for r in result["runs"]], "model_ready": result["model"]["ready"]}
        elif args.command == "execute":
            result = execute_plan(args.plan, budget_usd=args.budget_usd, budget_cny=args.budget_cny,
                                  max_requests=args.max_requests, max_credits=args.max_credits,
                                  budget_ledger=args.budget_ledger)
        elif args.command == "worker":
            run_worker(args.plan, args.variant)
            return
        else:
            result = verify_plan(args.plan, require_credentials=True)
            result = {"ready": True, "phase": result["phase"], "runs": len(result["runs"])}
    except (ValueError, OSError, RuntimeError, KeyError) as exc:
        parser.exit(2, f"error: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
