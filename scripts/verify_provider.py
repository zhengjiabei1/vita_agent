"""Verify an OpenAI-compatible model: tools, JSON, long context and usage.

This is a development preflight, not a benchmark. It sends a small fixed number
of requests through the same request/credit ledger as experiments, records only
metadata (no prompt or response text), and stops immediately on provider quota
exhaustion. Credentials come from the environment referenced by the model YAML.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time

import yaml

from vita_agent.budget import BudgetExceeded, BudgetLedger, UsageUnavailable, _Completions
from vita_agent.experiments import model_preflight

_ENV = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def _resolve(value):
    if isinstance(value, str):
        match = _ENV.fullmatch(value)
        if match:
            return os.environ.get(match[1])
        return value
    if isinstance(value, dict):
        return {key: _resolve(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve(item) for item in value]
    return value


def _merged_config(path: Path, model: str) -> dict:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    default = value.get("default", {})
    matches = [item for item in value.get("models", []) if item.get("name") == model]
    if len(matches) != 1:
        raise ValueError("Requested model must be declared exactly once")
    config = dict(default)
    config.update(matches[0])
    config.pop("name", None)
    return _resolve(config)


def _safe_error(exc: BaseException) -> dict:
    text = f"{exc}"
    text = re.sub(r"(?:Bearer\s+)[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text, flags=re.I)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "sk-[REDACTED]", text)
    return {"error_type": type(exc).__name__, "status_code": getattr(exc, "status_code", None),
            "error_message": text[:500]}


def _long_prompt(tokens: int) -> tuple[str, str]:
    needle = "NEEDLE-7F3A-2026"
    filler = "The quick brown fox jumps over the lazy dog. "
    target = max(1000, tokens * 4)
    half = max(1, (target - 200) // 2)
    left = (filler * (half // len(filler) + 1))[:half]
    right = (filler * (half // len(filler) + 1))[:half]
    prompt = (left + f"\nThe secret code is {needle}. Remember it exactly.\n" + right
              + "\nReturn only the secret code, with no explanation.")
    return prompt, needle


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-config", required=True, type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--long-context-tokens", type=int, default=120_000)
    parser.add_argument("--output", type=Path)
    quota = parser.add_mutually_exclusive_group(required=True)
    quota.add_argument("--max-requests", type=int)
    quota.add_argument("--budget-usd", type=float)
    args = parser.parse_args(argv)

    plan = model_preflight(args.model_config, args.model)
    if not plan["ready"]:
        parser.exit(2, "error: credential environment variable is not set\n")
    config = _merged_config(args.model_config.resolve(), args.model)
    from openai import OpenAI
    client = OpenAI(base_url=config["base_url"], api_key=config["api_key"], max_retries=0,
                    default_headers=config.get("extra_headers") or {})
    if plan["billing"] == "free":
        if plan["free_quota"]["unit"] != "request":
            parser.exit(2, "error: free credit-based models are not supported by this preflight\n")
        if args.max_requests is None or args.max_requests < 3:
            parser.exit(2, "error: --max-requests must be at least 3 for tool, JSON and long-context checks\n")
        ledger = BudgetLedger(args.ledger, limit=args.max_requests, currency="FREE",
                              pricing_basis="free_provider_quota", mode="requests")
        quota_mode, quota_limit = "requests", args.max_requests
    else:
        if args.budget_usd is None or not 0 < args.budget_usd < float("inf"):
            parser.exit(2, "error: paid models require a positive finite --budget-usd\n")
        if plan["currency"] != "USD":
            parser.exit(2, "error: this preflight currently supports USD paid models only\n")
        ledger = BudgetLedger(args.ledger, limit=args.budget_usd, currency="USD",
                              pricing_basis=plan["pricing_basis"], mode="currency")
        quota_mode, quota_limit = "currency", args.budget_usd
    completions = _Completions(client.chat.completions, ledger, {args.model: config})
    output = args.output or Path("artifacts/provider-preflight") / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + re.sub(r"[^A-Za-z0-9_.-]", "_", args.model) + ".json")
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
              "model": args.model, "endpoint_host": plan["endpoint_host"], "billing": plan["billing"],
              "quota_mode": quota_mode, "quota_limit": quota_limit,
              "output_limits": {"max_tokens": config.get("max_tokens")},
              "tests": [], "status": "running"}

    base = {key: config[key] for key in ("temperature", "extra_body", "top_p", "seed") if key in config}

    def call(name: str, **request) -> tuple[dict, str]:
        started = time.monotonic()
        try:
            response = completions.create(model=args.model, **base, **request)
        except Exception as exc:
            item = {"name": name, "ok": False, "latency_s": round(time.monotonic() - started, 3),
                    **_safe_error(exc)}
            report["tests"].append(item)
            if getattr(exc, "status_code", None) == 429 or "quota" in str(exc).casefold():
                report["status"] = "stopped_provider_quota_exhausted"
                raise
            return item, ""
        body = response.model_dump()
        text = (body.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        choice = (body.get("choices") or [{}])[0]
        item = {"name": name, "ok": True, "latency_s": round(time.monotonic() - started, 3),
                "usage": body.get("usage"), "content_chars": len(text),
                "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "tool_calls": len(choice.get("message", {}).get("tool_calls") or []),
                "finish_reason": choice.get("finish_reason")}
        report["tests"].append(item)
        return item, text

    try:
        tools = [{"type": "function", "function": {
            "name": "add_numbers", "description": "Add two integers.",
            "parameters": {"type": "object", "properties": {
                "a": {"type": "integer"}, "b": {"type": "integer"}},
                "required": ["a", "b"], "additionalProperties": False}}}]
        tool_test, _ = call(
            "tool_call", messages=[{"role": "user", "content": "Use the add_numbers tool with a=2 and b=3. You must use the tool."}],
            tools=tools, tool_choice="auto", max_tokens=256)
        tool_test["ok"] = bool(tool_test.get("tool_calls"))
        json_test, json_text = call("json_object", messages=[
            {"role": "user", "content": "Return exactly one JSON object: {\"ok\": true}."}],
            response_format={"type": "json_object"}, max_tokens=64)
        try:
            json_test["json_parse_ok"] = isinstance(json.loads(json_text), dict)
        except (ValueError, TypeError):
            json_test["json_parse_ok"] = False
        json_test["ok"] = json_test["json_parse_ok"]
        prompt, needle = _long_prompt(args.long_context_tokens)
        long_test, long_text = call("long_context", messages=[{"role": "user", "content": prompt}], max_tokens=512)
        long_test["expected_needle_sha256"] = hashlib.sha256(needle.encode()).hexdigest()
        long_test["contains_expected_needle"] = needle in long_text
        long_test["ok"] = long_test["contains_expected_needle"]
        report["status"] = "completed" if all(item["ok"] for item in report["tests"]) else "capability_incomplete"
    except (BudgetExceeded, UsageUnavailable) as exc:
        report["status"] = "stopped_quota_or_usage"
        report["error"] = _safe_error(exc)
    except Exception as exc:
        if report["status"] == "running":
            report["status"] = "stopped_provider_error"
        report["error"] = _safe_error(exc)
    finally:
        report["ledger"] = ledger.summary()
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({"status": report["status"], "model": args.model,
                      "requests_settled": report["ledger"]["counts"].get("settled", 0),
                      "tokens": {key: report["ledger"][key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")},
                      "output": str(output)}, ensure_ascii=False))
    raise SystemExit(0 if report["status"] == "completed" else 2)


if __name__ == "__main__":
    main()
