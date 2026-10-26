"""Shared API budget reservations for official VitaBench chat completions.

Two accounting modes are supported:

* ``currency`` reserves a conservative token-price estimate before each request,
  then settles from reported usage. This is the original paid-API mode.
* ``requests`` / ``credits`` reserve provider quota units for a genuinely free
  endpoint. Zero prices are never used as a stand-in for a free tariff: the
  model config must declare ``billing: free`` and the ledger records one unit
  per request (or a configured credit cost). Token usage is still recorded.

Network failures and missing usage keep their full reservation. SDK retries are
disabled. All official Agent, User and Evaluator calls using
``llm_utils._get_client`` share this accounting. Provider 429/quota exhaustion
halts the shared ledger so later phases cannot continue spending quota.

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
MODES = ("currency", "requests", "credits")
HALT_REASONS = ("provider_quota_exhausted", "reservation_overrun")


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
    if value not in ("USD", "CNY", "FREE"):
        raise ValueError("Budget currency must be explicitly USD, CNY or FREE; no exchange-rate conversion is performed")
    return value


def _mode(value: str, unit: str | None) -> tuple[str, str]:
    if value not in MODES:
        raise ValueError("Budget mode must be currency, requests or credits")
    if value == "currency":
        if unit not in (None, "token_cost"):
            raise ValueError("Currency mode always uses token_cost units")
        return value, "token_cost"
    expected = "request" if value == "requests" else "credit"
    if unit not in (None, expected):
        raise ValueError(f"{value} mode requires {expected} units")
    return value, expected


def _units(value: Any, name: str, *, mode: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    if mode == "requests" and float(value) != int(value):
        raise ValueError("Request quota units must be whole requests")
    return float(value)


def _provider_quota_error(exc: BaseException) -> bool:
    """Recognise a provider-side free-quota/rate-limit stop.

    This deliberately does not treat every network error as quota exhaustion;
    only an explicit 429/402 status or a known quota message halts the ledger.
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status in (402, 429):
        return True
    text = f"{exc}".casefold()
    return any(marker in text for marker in (
        "free usage limit", "quota", "rate limit", "insufficient account funds",
        "too many requests", "resource_exhausted"))


def _free_units(model_config: Mapping[str, Any], unit: str) -> float:
    if unit == "request":
        return 1.0
    quota = model_config.get("free_quota")
    if not isinstance(quota, Mapping):
        raise ValueError("Credit accounting requires an explicit free_quota block")
    value = quota.get("credit_per_request")
    return _units(value, "credit_per_request", mode="credits")


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
    mode: str = "currency"
    unit: str = "token_cost"

    @property
    def reserved_usd(self):
        if self.mode != "currency" or self.currency != "USD":
            raise AttributeError("This reservation is not denominated in USD")
        return self.reserved

    @property
    def reserved_cny(self):
        if self.mode != "currency" or self.currency != "CNY":
            raise AttributeError("This reservation is not denominated in CNY")
        return self.reserved

    @property
    def reserved_units(self):
        if self.mode == "currency":
            raise AttributeError("Currency reservations use token prices, not quota units")
        return self.reserved


class BudgetLedger:
    """Atomic reservations shared by all processes using the same SQLite path.

    A ledger's limit is immutable. Use a new ledger for a separately authorized
    experiment; changing the limit on an existing file is rejected.
    """

    def __init__(self, path: str | Path, limit_usd: float | None = None, *,
                 limit: float | None = None, currency: str = "USD",
                 pricing_basis: str = "configured_token_rates",
                 mode: str = "currency", unit: str | None = None,
                 request_limit: int | None = None):
        self.mode, self.unit = _mode(mode, unit)
        if request_limit is not None and (
                isinstance(request_limit, bool) or not isinstance(request_limit, int) or request_limit < 1):
            raise ValueError("request_limit must be a positive integer or null")
        self.request_limit = request_limit or 0
        if self.mode == "currency":
            self.currency = _currency(currency)
        else:
            if currency not in ("USD", "FREE"):
                raise ValueError("Free quota ledgers use currency='FREE', not a monetary currency")
            if pricing_basis not in ("free_provider_quota", "configured_token_rates"):
                raise ValueError("Free quota ledgers require pricing_basis='free_provider_quota'")
            self.currency = "FREE"
        if pricing_basis not in ("configured_token_rates", "peak_uncached_upper_bound", "free_provider_quota"):
            raise ValueError("Unsupported pricing basis")
        self.pricing_basis = pricing_basis
        if limit_usd is not None and (limit is not None or currency != "USD" or self.mode != "currency"):
            raise ValueError("Use limit=<amount>, currency='CNY' for CNY; do not mix limit_usd with a currency or limit")
        amount = limit_usd if limit_usd is not None else limit
        if self.mode == "requests" and isinstance(amount, float) and amount != int(amount):
            raise ValueError("Request quota must be a whole number of requests")
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
                    halted_reason TEXT NOT NULL DEFAULT '',
                    currency TEXT NOT NULL DEFAULT 'USD',
                    pricing_basis TEXT NOT NULL DEFAULT 'configured_token_rates',
                    mode TEXT NOT NULL DEFAULT 'currency',
                    unit TEXT NOT NULL DEFAULT 'token_cost',
                    request_limit INTEGER NOT NULL DEFAULT 0
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
            meta_columns = {row[1] for row in db.execute("PRAGMA table_info(budget_meta)")}
            for name, kind, default in (
                    ("currency", "TEXT", "'USD'"),
                    ("pricing_basis", "TEXT", "'configured_token_rates'"),
                    ("mode", "TEXT", "'currency'"),
                    ("unit", "TEXT", "'token_cost'"),
                    ("halted_reason", "TEXT", "''"),
                    ("request_limit", "INTEGER", "0")):
                if name not in meta_columns:
                    db.execute(f"ALTER TABLE budget_meta ADD COLUMN {name} {kind} NOT NULL DEFAULT {default}")
            columns = {row[1] for row in db.execute("PRAGMA table_info(budget_calls)")}
            for name, kind in (("cached_input_price", "REAL"), ("prompt_cache_hit_tokens", "INTEGER"),
                               ("prompt_cache_miss_tokens", "INTEGER"), ("cache_usage_reported", "INTEGER"),
                               ("response_id", "TEXT")):
                if name not in columns:
                    db.execute(f"ALTER TABLE budget_calls ADD COLUMN {name} {kind}")
            db.execute("INSERT OR IGNORE INTO budget_meta(singleton,limit_nano,currency,pricing_basis,mode,unit,request_limit) VALUES (1,?,?,?,?,?,?)",
                       (self.limit_nano, self.currency, self.pricing_basis, self.mode, self.unit, self.request_limit))
            existing = db.execute("SELECT limit_nano,currency,pricing_basis,mode,unit,request_limit FROM budget_meta WHERE singleton=1").fetchone()
            if (existing["limit_nano"] != self.limit_nano or existing["currency"] != self.currency
                    or existing["pricing_basis"] != self.pricing_basis or existing["mode"] != self.mode
                    or existing["unit"] != self.unit or existing["request_limit"] != self.request_limit):
                raise ValueError("Existing budget ledger has a different immutable limit, currency, pricing basis, mode, unit or request limit")

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
                input_price: float = 0.0, output_price: float = 0.0, *,
                cached_input_price: float | None = None, units: float | None = None) -> Reservation:
        if not isinstance(model, str) or not model.strip() or len(model) > 256:
            raise ValueError("A nonempty model name of at most 256 characters is required")
        input_token_bound = _tokens(input_token_bound, "input_token_bound", positive=True)
        output_token_limit = _tokens(output_token_limit, "output_token_limit", positive=True)
        if self.mode == "currency":
            if units is not None:
                raise ValueError("Currency reservations use token prices, not quota units")
            input_price = _positive(input_price, "prompt token price")
            output_price = _positive(output_price, "completion token price")
            if cached_input_price is not None:
                if (isinstance(cached_input_price, bool) or not isinstance(cached_input_price, (int, float))
                        or not math.isfinite(cached_input_price) or not 0 <= cached_input_price <= input_price):
                    raise ValueError("Cached input tariff must be between zero and the uncached tariff")
            amount = _charge(input_token_bound, output_token_limit, input_price, output_price)
        else:
            if input_price != 0 or output_price != 0 or cached_input_price is not None:
                raise ValueError("Free quota reservations must not supply fabricated token prices")
            units = _units(units, f"{self.unit} quota", mode=self.mode)
            amount = _nano(units)
            input_price = output_price = 0.0
        call_id = uuid4().hex
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            meta = db.execute("SELECT * FROM budget_meta WHERE singleton=1").fetchone()
            held = db.execute("SELECT COALESCE(SUM(held_nano),0) FROM budget_calls").fetchone()[0]
            if meta["halted"]:
                reason = meta["halted_reason"] or "observed reservation overrun"
                raise BudgetExceeded(f"Budget ledger halted after {reason}")
            if meta["request_limit"]:
                attempted = db.execute("SELECT COUNT(*) FROM budget_calls").fetchone()[0]
                if attempted >= meta["request_limit"]:
                    raise BudgetExceeded("Request quota exhausted for the next API request")
            if held + amount > meta["limit_nano"]:
                raise BudgetExceeded("Insufficient budget for the next bounded API request")
            db.execute("""INSERT INTO budget_calls
                (id,model,input_bound,output_limit,input_price,output_price,reserved_nano,held_nano,state,cached_input_price)
                VALUES (?,?,?,?,?,?,?,?,'pending',?)""",
                (call_id, model, input_token_bound, output_token_limit, input_price, output_price, amount, amount, cached_input_price))
        return Reservation(call_id, model, amount / NANOUNITS, input_token_bound, output_token_limit,
                           self.currency, self.mode, self.unit)

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
            if self.mode == "currency":
                actual = _charge(prompt, completion, row["input_price"], row["output_price"], cache_hit, row["cached_input_price"])
                overrun = actual > row["reserved_nano"] or prompt > row["input_bound"] or completion > row["output_limit"]
                state = "overrun" if overrun else "settled"
            else:
                # The request was admitted and completed: it consumes its quota
                # units regardless of token counts. Token-bound overruns are
                # recorded by the bounds columns but are not a spending overrun.
                actual = row["reserved_nano"]
                overrun, state = False, "settled"
            # Response IDs enable reconciliation with saved raw responses. Never
            # accept arbitrary provider payloads as ledger metadata.
            safe_response_id = (response_id if isinstance(response_id, str) and 0 < len(response_id) <= 256
                                and all(c.isalnum() or c in "-_.:" for c in response_id) else None)
            db.execute("""UPDATE budget_calls SET held_nano=?,actual_nano=?,prompt_tokens=?,completion_tokens=?,state=?,
                prompt_cache_hit_tokens=?,prompt_cache_miss_tokens=?,cache_usage_reported=?,response_id=? WHERE id=?""",
                       (actual, actual, prompt, completion, state, cache_hit, cache_miss, int(cache_reported), safe_response_id, call_id))
            if overrun:
                db.execute("UPDATE budget_meta SET halted=1,halted_reason='reservation_overrun' WHERE singleton=1")
        result = {"state": state, "actual": actual / NANOUNITS, "currency": self.currency,
                  "mode": self.mode, "unit": self.unit,
                  "prompt_tokens": prompt, "completion_tokens": completion,
                  "prompt_cache_hit_tokens": cache_hit, "prompt_cache_miss_tokens": cache_miss,
                  "cache_usage_reported": cache_reported,
                  "token_bound_exceeded": prompt > row["input_bound"] or completion > row["output_limit"]}
        if self.mode == "currency":
            result[f"actual_{self.currency.lower()}"] = actual / NANOUNITS
        else:
            result[f"actual_{self.unit}s"] = actual / NANOUNITS
        return result

    def halt(self, reason: str) -> None:
        if reason not in HALT_REASONS:
            raise ValueError("Halt reason must be a predefined non-sensitive code")
        with self._connect() as db:
            db.execute("UPDATE budget_meta SET halted=1,halted_reason=? WHERE singleton=1", (reason,))

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
            tokens = db.execute(
                "SELECT COALESCE(SUM(prompt_tokens),0) AS prompt, COALESCE(SUM(completion_tokens),0) AS completion "
                "FROM budget_calls WHERE state IN ('settled','overrun')").fetchone()
        held = sum(r["held"] for r in rows)
        measured = sum(r["actual"] or 0 for r in rows) / NANOUNITS
        complete = not any(r["state"] in ("pending", "uncertain") for r in rows)
        amounts = {"limit": meta["limit_nano"] / NANOUNITS, "held": held / NANOUNITS,
                   "remaining": max(0, meta["limit_nano"] - held) / NANOUNITS,
                   "measured": measured, "actual_total": measured if complete else None}
        token_stats = {"prompt_tokens": tokens["prompt"], "completion_tokens": tokens["completion"],
                       "total_tokens": tokens["prompt"] + tokens["completion"]}
        requests = sum(r["n"] for r in rows)
        base = {"mode": meta["mode"], "unit": meta["unit"], "currency": self.currency,
                "pricing_basis": self.pricing_basis, **token_stats,
                "requests": requests,
                "request_limit": meta["request_limit"] or None,
                "remaining_requests": (max(0, meta["request_limit"] - requests)
                                       if meta["request_limit"] else None),
                "cost_complete": complete,
                "halted": bool(meta["halted"]), "halted_reason": meta["halted_reason"] or None,
                "counts": {r["state"]: r["n"] for r in rows},
                "scope": "All chat calls routed through the official _get_client; excludes provider-side fees"}
        if self.mode == "currency":
            aliases = {f"{key}_{self.currency.lower()}": value for key, value in amounts.items()}
            return {**amounts, **aliases, **base,
                    "token_priced_total": measured if complete else None,
                    "invoice_total": None, "invoice_verified": False,
                    "cost_note": "Token usage valued at configured rates; not a provider invoice. Cache/time discounts and provider fees may differ.",
                    "cache_accounting": "New calls apply configured cached input prices only to validated provider cache counts. Historical settled records are unchanged; reservation basis remains worst-case uncached."}
        unit_key = "requests" if self.unit == "request" else "credits"
        aliases = {f"{key}_{unit_key}": value for key, value in amounts.items()}
        return {**amounts, **aliases, **base,
                "token_priced_total": None, "invoice_total": None, "invoice_verified": False,
                "cost_note": "Free provider quota accounting. Token usage is recorded; no cash price is invented.",
                "cache_accounting": "Free mode records reported cache counts when present; no token price is applied.",
                "quota_note": "Provider-side daily/monthly quota remains authoritative; this ledger only prevents exceeding the declared local limit."}


def request_bounds(request: Mapping[str, Any], model_config: Mapping[str, Any], *,
                   currency: str = "USD", mode: str = "currency") -> tuple[int, int, float, float]:
    """Validate a supported text-only, one-completion request and its tariff.

    In ``requests``/``credits`` mode no token price is required or invented:
    the first two bounds are still used for usage monitoring, while the two
    price fields are explicit zeros that the free-mode reservation ignores.

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
    if mode in ("requests", "credits"):
        if model_config.get("billing") != "free":
            raise ValueError("Free quota mode requires a model config with billing: free")
        input_price = output_price = 0.0
    elif mode == "currency":
        input_price, output_price = _tariff(model_config, currency)
    else:
        raise ValueError("Unknown budget mode")
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
        config = self.model_configs[model]
        if self.ledger.mode == "currency":
            bounds = request_bounds(request, config, currency=self.ledger.currency)
            cached_price = _cached_price(config, self.ledger.currency)
            reservation = self.ledger.reserve(model, *bounds, cached_input_price=cached_price)
        else:
            bounds = request_bounds(request, config, mode=self.ledger.mode)
            reservation = self.ledger.reserve(model, *bounds,
                                              units=_free_units(config, self.ledger.unit))
        try:
            response = self.original.create(**request)
        except Exception as exc:
            self.ledger.mark_uncertain(reservation.id)
            if _provider_quota_error(exc):
                self.ledger.halt("provider_quota_exhausted")
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
                         pricing_basis: str = "configured_token_rates",
                         mode: str = "currency", unit: str | None = None,
                         request_limit: int | None = None) -> BudgetLedger:
    """Patch the official client factory; install before launching benchmark runs.

    Returns the ledger for reporting. Reinstallation targets the same original
    factory rather than nesting wrappers. Client SDK retries are forced to zero.
    Configure VITA_MODEL_CONFIG_PATH before calling: upstream loads model config
    on import. This function performs no network request.

    For a genuinely free endpoint use ``mode='requests'`` or ``mode='credits'``
    and a positive request/credit limit; do not pass zero token prices.
    """
    from vita.utils import llm_utils
    ledger = BudgetLedger(path, limit_usd, limit=limit, currency=currency,
                          pricing_basis=pricing_basis, mode=mode, unit=unit,
                          request_limit=request_limit)
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
        if ledger.mode != "currency":
            # A free tier's cash cost is zero; quota consumption is tracked by
            # the ledger. This is not a zero-price paid tariff.
            return 0.0
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
    llm_utils.BUDGET_MODE = ledger.mode
    llm_utils.BUDGET_UNIT = ledger.unit
    return ledger
