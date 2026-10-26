"""Shared API budget reservations for official VitaBench chat completions.

The ledger reserves a conservative token-byte estimate before each API request,
then settles from reported token usage. Network failures and missing usage keep
their full reservation. SDK retries are disabled. All official Agent, User and
Evaluator calls using ``llm_utils._get_client`` share this accounting.

This is an admission control guard, not a provider billing guarantee: tariffs,
tokenization, hidden usage and provider-side retries can differ. Any observed
reservation overrun halts subsequent requests. Use a provider-side spending cap
as well. No prompts, API keys, response text or endpoint URLs enter the ledger.
"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from decimal import Decimal, ROUND_CEILING
import json
import math
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from typing import Any, Mapping
from uuid import uuid4


NANOUNITS = 1_000_000_000


class BudgetExceeded(RuntimeError):
    pass


class UsageUnavailable(RuntimeError):
    pass


def _positive(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return float(value)


def _tokens(value: Any, name: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < (1 if positive else 0):
        raise ValueError(f"{name} must be a {'positive' if positive else 'nonnegative'} integer")
    return value


def _nano(value: float) -> int:
    return int((Decimal(str(value)) * NANOUNITS).to_integral_value(rounding=ROUND_CEILING))


def _charge(prompt: int, completion: int, input_price: float, output_price: float,
            cached_tokens: int = 0, cached_input_price: float | None = None) -> int:
    cache_price = input_price if cached_input_price is None else cached_input_price
    value = (Decimal(prompt - cached_tokens) * Decimal(str(input_price))
             + Decimal(cached_tokens) * Decimal(str(cache_price))
             + Decimal(completion) * Decimal(str(output_price))) / 1_000_000
    return int((value * NANOUNITS).to_integral_value(rounding=ROUND_CEILING))


def _currency(value: str) -> str:
    if value not in ("USD", "CNY"):
        raise ValueError("Budget currency must be explicitly USD or CNY; no exchange-rate conversion is performed")
    return value


def _tariff(model_config: Mapping[str, Any], currency: str) -> tuple[float, float]:
    key = "cost_1m_token_dollar" if _currency(currency) == "USD" else "cost_1m_token_cny"
    prices = model_config.get(key)
    if not isinstance(prices, Mapping):
        raise ValueError(f"Explicit model token prices in {currency} are required ({key})")
    return (_positive(prices.get("prompt_price"), "prompt token price"),
            _positive(prices.get("completion_price"), "completion token price"))


def _cached_price(model_config: Mapping[str, Any], currency: str) -> float | None:
    key = "cost_1m_token_dollar" if _currency(currency) == "USD" else "cost_1m_token_cny"
    prices = model_config.get(key, {})
    value = prices.get("cached_prompt_price")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("Cached prompt price must be finite and nonnegative")
    if value > _positive(prices.get("prompt_price"), "prompt token price"):
        raise ValueError("Cached prompt price cannot exceed the uncached reservation tariff")
    return float(value)


def _cache_counts(usage: Mapping[str, Any], prompt: int) -> tuple[int, int, bool]:
    """Missing cache telemetry conservatively means all input was uncached."""
    if usage.get("prompt_cache_hit_tokens") is None or usage.get("prompt_cache_miss_tokens") is None:
        return 0, prompt, False
    hit = _tokens(usage["prompt_cache_hit_tokens"], "prompt_cache_hit_tokens")
    miss = _tokens(usage["prompt_cache_miss_tokens"], "prompt_cache_miss_tokens")
    if hit + miss != prompt:
        raise ValueError("Reported cache hits and misses must sum to prompt_tokens")
    return hit, miss, True


@dataclass(frozen=True)
class Reservation:
    id: str
    model: str
    reserved: float
    input_token_bound: int
    output_token_limit: int
    currency: str = "USD"

    @property
    def reserved_usd(self):
        if self.currency != "USD":
            raise AttributeError("This reservation is not denominated in USD")
        return self.reserved

    @property
    def reserved_cny(self):
        if self.currency != "CNY":
            raise AttributeError("This reservation is not denominated in CNY")
        return self.reserved


class BudgetLedger:
    """Atomic reservations shared by all processes using the same SQLite path.

    A ledger's limit is immutable. Use a new ledger for a separately authorized
    experiment; changing the limit on an existing file is rejected.
    """

    def __init__(self, path: str | Path, limit_usd: float | None = None, *,
                 limit: float | None = None, currency: str = "USD",
                 pricing_basis: str = "configured_token_rates"):
        self.currency = _currency(currency)
        if pricing_basis not in ("configured_token_rates", "peak_uncached_upper_bound"):
            raise ValueError("Unsupported pricing basis")
        self.pricing_basis = pricing_basis
        if limit_usd is not None and (limit is not None or currency != "USD"):
            raise ValueError("Use limit=<amount>, currency='CNY' for CNY; do not mix limit_usd with a currency or limit")
        amount = limit_usd if limit_usd is not None else limit
        self.path = str(Path(path).resolve())
        if str(path) == ":memory:":
            raise ValueError("A persistent file is required for shared budget accounting")
        self.limit_nano = _nano(_positive(amount, "limit"))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS budget_meta (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    limit_nano INTEGER NOT NULL, halted INTEGER NOT NULL DEFAULT 0,
                    currency TEXT NOT NULL DEFAULT 'USD',
                    pricing_basis TEXT NOT NULL DEFAULT 'configured_token_rates'
                );
                CREATE TABLE IF NOT EXISTS budget_calls (
                    id TEXT PRIMARY KEY, model TEXT NOT NULL,
                    input_bound INTEGER NOT NULL, output_limit INTEGER NOT NULL,
                    input_price REAL NOT NULL, output_price REAL NOT NULL,
                    reserved_nano INTEGER NOT NULL, held_nano INTEGER NOT NULL,
                    actual_nano INTEGER, prompt_tokens INTEGER, completion_tokens INTEGER,
                    state TEXT NOT NULL, reason TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
            """)
            db.execute("BEGIN IMMEDIATE")
            if "currency" not in {row[1] for row in db.execute("PRAGMA table_info(budget_meta)")}:
                db.execute("ALTER TABLE budget_meta ADD COLUMN currency TEXT NOT NULL DEFAULT 'USD'")
            if "pricing_basis" not in {row[1] for row in db.execute("PRAGMA table_info(budget_meta)")}:
                db.execute("ALTER TABLE budget_meta ADD COLUMN pricing_basis TEXT NOT NULL DEFAULT 'configured_token_rates'")
            columns = {row[1] for row in db.execute("PRAGMA table_info(budget_calls)")}
            for name, kind in (("cached_input_price", "REAL"), ("prompt_cache_hit_tokens", "INTEGER"),
                               ("prompt_cache_miss_tokens", "INTEGER"), ("cache_usage_reported", "INTEGER"),
                               ("response_id", "TEXT")):
                if name not in columns:
                    db.execute(f"ALTER TABLE budget_calls ADD COLUMN {name} {kind}")
            db.execute("INSERT OR IGNORE INTO budget_meta(singleton,limit_nano,currency,pricing_basis) VALUES (1,?,?,?)", (self.limit_nano, self.currency, self.pricing_basis))
            existing = db.execute("SELECT limit_nano,currency,pricing_basis FROM budget_meta WHERE singleton=1").fetchone()
            if existing["limit_nano"] != self.limit_nano or existing["currency"] != self.currency or existing["pricing_basis"] != self.pricing_basis:
                raise ValueError("Existing budget ledger has a different immutable spending limit, currency or pricing basis")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def reserve(self, model: str, input_token_bound: int, output_token_limit: int,
                input_price: float, output_price: float, *, cached_input_price: float | None = None) -> Reservation:
        if not isinstance(model, str) or not model.strip() or len(model) > 256:
            raise ValueError("A nonempty model name of at most 256 characters is required")
        input_token_bound = _tokens(input_token_bound, "input_token_bound", positive=True)
        output_token_limit = _tokens(output_token_limit, "output_token_limit", positive=True)
        input_price = _positive(input_price, "prompt token price")
        output_price = _positive(output_price, "completion token price")
        if cached_input_price is not None:
            if (isinstance(cached_input_price, bool) or not isinstance(cached_input_price, (int, float))
                    or not math.isfinite(cached_input_price) or not 0 <= cached_input_price <= input_price):
                raise ValueError("Cached input tariff must be between zero and the uncached tariff")
        amount = _charge(input_token_bound, output_token_limit, input_price, output_price)
        call_id = uuid4().hex
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            meta = db.execute("SELECT * FROM budget_meta WHERE singleton=1").fetchone()
            held = db.execute("SELECT COALESCE(SUM(held_nano),0) FROM budget_calls").fetchone()[0]
            if meta["halted"]:
                raise BudgetExceeded("Budget ledger halted after an observed reservation overrun")
            if held + amount > meta["limit_nano"]:
                raise BudgetExceeded("Insufficient budget for the next bounded API request")
            db.execute("""INSERT INTO budget_calls
                (id,model,input_bound,output_limit,input_price,output_price,reserved_nano,held_nano,state,cached_input_price)
                VALUES (?,?,?,?,?,?,?,?,'pending',?)""",
                (call_id, model, input_token_bound, output_token_limit, input_price, output_price, amount, amount, cached_input_price))
        return Reservation(call_id, model, amount / NANOUNITS, input_token_bound, output_token_limit, self.currency)

    def settle(self, call_id: str, usage: Mapping[str, Any] | None, *, response_id: str | None = None) -> dict:
        try:
            if not isinstance(usage, Mapping):
                raise ValueError("Missing usage")
            prompt = _tokens(usage.get("prompt_tokens"), "prompt_tokens")
            completion = _tokens(usage.get("completion_tokens"), "completion_tokens")
            cache_hit, cache_miss, cache_reported = _cache_counts(usage, prompt)
        except ValueError:
            self.mark_uncertain(call_id, "missing_or_invalid_usage")
            raise UsageUnavailable("Provider did not return valid usage; full reservation retained") from None
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM budget_calls WHERE id=?", (call_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown budget reservation")
            if row["state"] != "pending":
                raise ValueError("Reservation is already terminal; settlement replay is not allowed")
            actual = _charge(prompt, completion, row["input_price"], row["output_price"], cache_hit, row["cached_input_price"])
            overrun = actual > row["reserved_nano"] or prompt > row["input_bound"] or completion > row["output_limit"]
            state = "overrun" if overrun else "settled"
            # Response IDs enable reconciliation with saved raw responses. Never
            # accept arbitrary provider payloads as ledger metadata.
            safe_response_id = (response_id if isinstance(response_id, str) and 0 < len(response_id) <= 256
                                and all(c.isalnum() or c in "-_.:" for c in response_id) else None)
            db.execute("""UPDATE budget_calls SET held_nano=?,actual_nano=?,prompt_tokens=?,completion_tokens=?,state=?,
                prompt_cache_hit_tokens=?,prompt_cache_miss_tokens=?,cache_usage_reported=?,response_id=? WHERE id=?""",
                       (actual, actual, prompt, completion, state, cache_hit, cache_miss, int(cache_reported), safe_response_id, call_id))
            if overrun:
                db.execute("UPDATE budget_meta SET halted=1 WHERE singleton=1")
        return {"state": state, "actual": actual / NANOUNITS, "currency": self.currency,
                f"actual_{self.currency.lower()}": actual / NANOUNITS,
                "prompt_tokens": prompt, "completion_tokens": completion,
                "prompt_cache_hit_tokens": cache_hit, "prompt_cache_miss_tokens": cache_miss,
                "cache_usage_reported": cache_reported}

    def mark_uncertain(self, call_id: str, reason: str = "request_failed") -> None:
        if reason not in ("request_failed", "missing_or_invalid_usage", "response_unreadable"):
            raise ValueError("Uncertainty reason must be a predefined non-sensitive code")
        with self._connect() as db:
            changed = db.execute("UPDATE budget_calls SET state='uncertain',reason=? WHERE id=? AND state='pending'",
                                 (reason, call_id)).rowcount
            if not changed:
                row = db.execute("SELECT state FROM budget_calls WHERE id=?", (call_id,)).fetchone()
                if row is None:
                    raise ValueError("Unknown budget reservation")

    def summary(self) -> dict:
        with self._connect() as db:
            meta = db.execute("SELECT * FROM budget_meta WHERE singleton=1").fetchone()
            rows = db.execute("SELECT state,COUNT(*) AS n,SUM(held_nano) AS held,SUM(actual_nano) AS actual FROM budget_calls GROUP BY state").fetchall()
        held = sum(r["held"] for r in rows)
        measured = sum(r["actual"] or 0 for r in rows) / NANOUNITS
        complete = not any(r["state"] in ("pending", "uncertain") for r in rows)
        amounts = {"limit": meta["limit_nano"] / NANOUNITS, "held": held / NANOUNITS,
                   "remaining": max(0, meta["limit_nano"] - held) / NANOUNITS,
                   "measured": measured, "actual_total": measured if complete else None}
        return {**amounts, **{f"{key}_{self.currency.lower()}": value for key, value in amounts.items()},
                "currency": self.currency,
                "pricing_basis": self.pricing_basis,
                "token_priced_total": measured if complete else None,
                "invoice_total": None, "invoice_verified": False,
                "cost_note": "Token usage valued at configured rates; not a provider invoice. Cache/time discounts and provider fees may differ.",
                "cache_accounting": "New calls apply configured cached input prices only to validated provider cache counts. Historical settled records are unchanged; reservation basis remains worst-case uncached.",
                "cost_complete": complete,
                "halted": bool(meta["halted"]), "counts": {r["state"]: r["n"] for r in rows},
                "scope": "All chat calls routed through the official _get_client; excludes provider-side fees"}


def request_bounds(request: Mapping[str, Any], model_config: Mapping[str, Any], *, currency: str = "USD") -> tuple[int, int, float, float]:
    """Validate a supported text-only, one-completion request and its tariff.

    UTF-8 bytes upper-bound ordinary text BPE token counts; extra protocol/tool
    overhead is conservatively reserved. This assumption is monitored against
    reported usage, not advertised as a universal provider-tokenizer guarantee.
    """
    extra_body = request.get("extra_body")
    if extra_body and extra_body != {"thinking": {"type": "disabled"}}:
        raise ValueError("Budgeted extra_body only permits explicit thinking disabled; cost-changing overrides are rejected")
    if request.get("stream") or isinstance(request.get("n", 1), bool) or request.get("n", 1) != 1:
        raise ValueError("Budgeted calls require non-streaming single completions")
    messages = request.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("Budgeted calls require a nonempty text message list")
    for message in messages:
        if not isinstance(message, dict) or (message.get("content") is not None and not isinstance(message["content"], str)):
            raise ValueError("Budget estimation supports text-only messages")
    caps = [request[key] for key in ("max_tokens", "max_completion_tokens") if request.get(key) is not None]
    if not caps:
        raise ValueError("An explicit max_tokens or max_completion_tokens cap is required")
    output_limit = max(_tokens(cap, "output token cap", positive=True) for cap in caps)
    input_price, output_price = _tariff(model_config, currency)
    payload = {key: request[key] for key in ("messages", "tools", "tool_choice", "response_format") if key in request}
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    tool_count = len(request.get("tools") or [])
    input_bound = len(encoded) + 1024 + 64 * len(messages) + 256 * tool_count
    return input_bound, output_limit, input_price, output_price


class _Completions:
    def __init__(self, original, ledger: BudgetLedger, model_configs: Mapping[str, Any]):
        self.original, self.ledger, self.model_configs = original, ledger, model_configs

    def create(self, **request):
        model = request.get("model")
        if not isinstance(model, str) or model not in self.model_configs:
            raise ValueError("Budgeted model must have explicit model configuration")
        bounds = request_bounds(request, self.model_configs[model], currency=self.ledger.currency)
        cached_price = _cached_price(self.model_configs[model], self.ledger.currency)
        reservation = self.ledger.reserve(model, *bounds, cached_input_price=cached_price)
        try:
            response = self.original.create(**request)
        except Exception:
            self.ledger.mark_uncertain(reservation.id)
            raise
        try:
            body = response.model_dump()
            if not isinstance(body, Mapping):
                raise ValueError("Response is not a mapping")
        except Exception:
            self.ledger.mark_uncertain(reservation.id, "response_unreadable")
            raise UsageUnavailable("Cannot inspect provider usage; full reservation retained") from None
        self.ledger.settle(reservation.id, body.get("usage"), response_id=body.get("id"))
        return response


def install_budget_guard(path: str | Path, limit_usd: float | None = None, *,
                         limit: float | None = None, currency: str = "USD",
                         pricing_basis: str = "configured_token_rates") -> BudgetLedger:
    """Patch the official client factory; install before launching benchmark runs.

    Returns the ledger for reporting. Reinstallation targets the same original
    factory rather than nesting wrappers. Client SDK retries are forced to zero.
    Configure VITA_MODEL_CONFIG_PATH before calling: upstream loads model config
    on import. This function performs no network request.
    """
    from vita.utils import llm_utils
    ledger = BudgetLedger(path, limit_usd, limit=limit, currency=currency, pricing_basis=pricing_basis)
    original = getattr(llm_utils._get_client, "_budget_original", llm_utils._get_client)

    def guarded_factory(base_url, api_key, max_retries):
        client = original(base_url=base_url, api_key=api_key, max_retries=0)
        return SimpleNamespace(chat=SimpleNamespace(completions=_Completions(
            client.chat.completions, ledger, llm_utils.models)))

    guarded_factory._budget_original = original
    llm_utils._get_client = guarded_factory

    def response_cost(usage, model):
        # Upstream agent_cost has no embedded currency: the experiment manifest
        # MUST record ledger.currency, and comparisons must reject mismatches.
        if not isinstance(usage, Mapping):
            raise UsageUnavailable("Valid usage is required for cost reporting")
        prompt = _tokens(usage.get("prompt_tokens"), "prompt_tokens")
        completion = _tokens(usage.get("completion_tokens"), "completion_tokens")
        cache_hit, _, _ = _cache_counts(usage, prompt)
        ip, op = _tariff(llm_utils.models.get(model, {}), ledger.currency)
        cached_price = _cached_price(llm_utils.models.get(model, {}), ledger.currency)
        return _charge(prompt, completion, ip, op, cache_hit, cached_price) / NANOUNITS

    def response_usage(response_dict):
        usage = response_dict.get("usage")
        if not isinstance(usage, Mapping):
            return None
        fields = ("prompt_tokens", "completion_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")
        return {key: usage[key] for key in fields if key in usage}

    llm_utils.get_response_cost = response_cost
    llm_utils.get_response_usage = response_usage
    llm_utils.BUDGET_CURRENCY = ledger.currency
    return ledger
