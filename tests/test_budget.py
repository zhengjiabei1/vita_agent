from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from vita_agent.budget import BudgetExceeded, BudgetLedger, UsageUnavailable, _Completions, request_bounds


CONFIG = {"cost_1m_token_dollar": {"prompt_price": 2.0, "completion_price": 8.0}}


def test_reserve_and_release_unused_allowance(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.db", 1)
    reservation = ledger.reserve("model", 1000, 100, 2, 8)
    assert reservation.reserved_usd == pytest.approx(0.0028)
    settled = ledger.settle(reservation.id, {"prompt_tokens": 100, "completion_tokens": 10})
    assert settled["actual_usd"] == pytest.approx(0.00028)
    assert ledger.summary()["remaining_usd"] == pytest.approx(0.99972)
    with pytest.raises(ValueError, match="terminal"):
        ledger.settle(reservation.id, {"prompt_tokens": 100, "completion_tokens": 10})


def test_multiple_connections_cannot_reserve_past_limit(tmp_path):
    path = tmp_path / "budget.db"
    BudgetLedger(path, 0.01)
    def reserve(_):
        ledger = BudgetLedger(path, 0.01)
        try:
            ledger.reserve("model", 1000, 1000, 2, 8)
            return True
        except BudgetExceeded:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, range(8))) == 1
    assert BudgetLedger(path, 0.01).summary()["held_usd"] == 0.01


def test_uncertain_keeps_full_reservation_and_persists(tmp_path):
    path = tmp_path / "budget.db"
    ledger = BudgetLedger(path, 0.01)
    reservation = ledger.reserve("model", 1000, 1000, 2, 8)
    with pytest.raises(UsageUnavailable):
        ledger.settle(reservation.id, None)
    reopened = BudgetLedger(path, 0.01)
    assert reopened.summary()["counts"] == {"uncertain": 1}
    assert reopened.summary()["actual_total_usd"] is None
    assert reopened.summary()["cost_complete"] is False
    with pytest.raises(BudgetExceeded):
        reopened.reserve("model", 1, 1, 2, 8)


def test_overrun_halts_future_calls_even_with_money_remaining(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.db", 100)
    reservation = ledger.reserve("model", 1, 1, 2, 8)
    report = ledger.settle(reservation.id, {"prompt_tokens": 10, "completion_tokens": 10})
    assert report["state"] == "overrun"
    assert ledger.summary()["halted"]
    with pytest.raises(BudgetExceeded, match="halted"):
        ledger.reserve("model", 1, 1, 2, 8)


def test_limit_immutable_and_invalid_inputs(tmp_path):
    path = tmp_path / "budget.db"
    BudgetLedger(path, 1)
    with pytest.raises(ValueError, match="immutable"):
        BudgetLedger(path, 2)
    for invalid in (True, 0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            BudgetLedger(tmp_path / "bad.db", invalid)


def test_request_requires_prices_output_cap_and_text():
    request = {"model": "model", "messages": [{"role": "user", "content": "你好"}], "max_tokens": 100}
    input_bound, cap, ip, op = request_bounds(request, CONFIG)
    assert input_bound > len("你好".encode()) and cap == 100 and (ip, op) == (2, 8)
    for patch in ({"max_tokens": None}, {"stream": True}, {"n": 2}, {"extra_body": {"max_tokens": 999}},
                  {"messages": [{"role": "user", "content": [{"type": "image_url"}]}]}):
        with pytest.raises(ValueError):
            request_bounds({**request, **patch}, CONFIG)
    for config in ({}, {"cost_1m_token_dollar": {"prompt_price": 0, "completion_price": 8}},
                   {"cost_1m_token_dollar": {"prompt_price": True, "completion_price": 8}}):
        with pytest.raises(ValueError):
            request_bounds(request, config)


def test_wrapper_does_not_record_prompt_or_secret(tmp_path):
    path = tmp_path / "budget.db"
    ledger = BudgetLedger(path, 1)
    raw = SimpleNamespace(create=lambda **kw: SimpleNamespace(model_dump=lambda: {"usage": {"prompt_tokens": 20, "completion_tokens": 5}}))
    wrapped = _Completions(raw, ledger, {"model": CONFIG})
    wrapped.create(model="model", messages=[{"role": "user", "content": "sensitive-user-prompt"}],
                   max_tokens=100, extra_headers={"Authorization": "secret-must-not-be-recorded"})
    with sqlite3.connect(path) as db:
        dump = "\n".join(db.iterdump())
    assert "sensitive-user-prompt" not in dump and "secret-must-not-be-recorded" not in dump
    assert ledger.summary()["counts"] == {"settled": 1}


def test_network_error_is_uncertain_and_not_retried(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.db", 1)
    calls = []
    def fail(**kw):
        calls.append(1)
        raise TimeoutError("possibly billed")
    wrapped = _Completions(SimpleNamespace(create=fail), ledger, {"model": CONFIG})
    with pytest.raises(TimeoutError):
        wrapped.create(model="model", messages=[{"role": "user", "content": "hello"}], max_tokens=100)
    assert calls == [1] and ledger.summary()["counts"] == {"uncertain": 1}


def test_missing_usage_fails_instead_of_reporting_free_generation(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.db", 1)
    raw = SimpleNamespace(create=lambda **kw: SimpleNamespace(model_dump=lambda: {}))
    wrapped = _Completions(raw, ledger, {"model": CONFIG})
    with pytest.raises(UsageUnavailable):
        wrapped.create(model="model", messages=[{"role": "user", "content": "hello"}], max_tokens=100)
    assert ledger.summary()["held_usd"] > 0


def test_summary_is_json_serializable(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.db", 0.1)
    assert json.loads(json.dumps(ledger.summary()))["limit_usd"] == 0.1


def test_installer_disables_sdk_retries_and_does_not_nest(monkeypatch, tmp_path):
    from vita_agent.budget import install_budget_guard
    factory_calls = []
    def original_factory(**kwargs):
        factory_calls.append(kwargs)
        completion = SimpleNamespace(create=lambda **kw: SimpleNamespace(model_dump=lambda:
            {"usage": {"prompt_tokens": 10, "completion_tokens": 1}}))
        return SimpleNamespace(chat=SimpleNamespace(completions=completion))
    fake = SimpleNamespace(_get_client=original_factory, models={"model": CONFIG})
    monkeypatch.setitem(sys.modules, "vita.utils", SimpleNamespace(llm_utils=fake))
    install_budget_guard(tmp_path / "budget.db", 1)
    ledger = install_budget_guard(tmp_path / "budget.db", 1)
    client = fake._get_client(base_url="https://provider.invalid/v1", api_key="not-recorded", max_retries=5)
    client.chat.completions.create(model="model", messages=[{"role": "user", "content": "hello"}], max_tokens=10)
    assert factory_calls[0]["max_retries"] == 0
    assert ledger.summary()["counts"] == {"settled": 1}


def test_cny_cap_has_no_dollar_aliases_or_conversion(tmp_path):
    ledger = BudgetLedger(tmp_path / "cny.db", limit=20, currency="CNY", pricing_basis="peak_uncached_upper_bound")
    reservation = ledger.reserve("deepseek-flash", 1_000_000, 1_000_000, 2, 8)
    assert reservation.reserved_cny == 10
    with pytest.raises(AttributeError):
        _ = reservation.reserved_usd
    settled = ledger.settle(reservation.id, {"prompt_tokens": 100_000, "completion_tokens": 10_000})
    assert settled["actual_cny"] == 0.28 and "actual_usd" not in settled
    summary = ledger.summary()
    assert summary["currency"] == "CNY" and summary["limit_cny"] == 20
    assert summary["token_priced_total"] == 0.28 and summary["invoice_total"] is None
    assert summary["pricing_basis"] == "peak_uncached_upper_bound"
    assert not any(key.endswith("_usd") for key in summary)
    with pytest.raises(ValueError, match="immutable"):
        BudgetLedger(tmp_path / "cny.db", 20)


def test_cny_prices_require_cny_config_and_safe_thinking_override():
    request = {"model": "deepseek-flash", "messages": [{"role": "user", "content": "hello"}],
               "max_tokens": 2048, "extra_body": {"thinking": {"type": "disabled"}}}
    config = {"cost_1m_token_cny": {"prompt_price": 2, "completion_price": 8}}
    assert request_bounds(request, config, currency="CNY")[2:] == (2, 8)
    with pytest.raises(ValueError, match="CNY"):
        request_bounds(request, CONFIG, currency="CNY")
    with pytest.raises(ValueError, match="thinking disabled"):
        request_bounds({**request, "extra_body": {"thinking": {"type": "enabled"}}}, config, currency="CNY")


def test_install_cny_patches_official_cost_without_fake_usd(monkeypatch, tmp_path):
    from vita_agent.budget import install_budget_guard
    fake = SimpleNamespace(_get_client=lambda **kw: None,
        models={"deepseek-flash": {"cost_1m_token_cny": {"prompt_price": 2, "completion_price": 8}}})
    monkeypatch.setitem(sys.modules, "vita.utils", SimpleNamespace(llm_utils=fake))
    install_budget_guard(tmp_path / "cny.db", limit=20, currency="CNY", pricing_basis="peak_uncached_upper_bound")
    assert fake.get_response_cost({"prompt_tokens": 100_000, "completion_tokens": 10_000}, "deepseek-flash") == 0.28
    assert fake.BUDGET_CURRENCY == "CNY"


def test_reject_ambiguous_or_unknown_currency(tmp_path):
    with pytest.raises(ValueError):
        BudgetLedger(tmp_path / "b.db", 20, currency="CNY")
    with pytest.raises(ValueError):
        BudgetLedger(tmp_path / "b.db", limit=20, currency="RMB")


def test_cache_settlement_keeps_uncached_reservation_and_records_response(tmp_path):
    ledger = BudgetLedger(tmp_path / "cache.db", limit=20, currency="CNY")
    reservation = ledger.reserve("deepseek-flash", 1_000_000, 1000, 2, 8, cached_input_price=0.04)
    assert reservation.reserved_cny == 2.008
    settled = ledger.settle(reservation.id, {"prompt_tokens": 1_000_000, "completion_tokens": 1000,
        "prompt_cache_hit_tokens": 990_000, "prompt_cache_miss_tokens": 10_000}, response_id="chatcmpl-real-response-1")
    assert settled["actual_cny"] == pytest.approx(0.0676)
    with sqlite3.connect(ledger.path) as db:
        row = db.execute("SELECT response_id,prompt_cache_hit_tokens,prompt_cache_miss_tokens,cached_input_price FROM budget_calls").fetchone()
    assert row == ("chatcmpl-real-response-1", 990_000, 10_000, 0.04)
    assert ledger.summary()["held_cny"] == pytest.approx(0.0676)


def test_invalid_cache_telemetry_retains_full_reservation(tmp_path):
    ledger = BudgetLedger(tmp_path / "cache.db", limit=20, currency="CNY")
    reservation = ledger.reserve("model", 1000, 100, 2, 8, cached_input_price=0.04)
    with pytest.raises(UsageUnavailable):
        ledger.settle(reservation.id, {"prompt_tokens": 1000, "completion_tokens": 10,
            "prompt_cache_hit_tokens": 990, "prompt_cache_miss_tokens": 20})
    assert ledger.summary()["held_cny"] == reservation.reserved_cny
    assert ledger.summary()["actual_total"] is None


def test_missing_cache_counts_defaults_to_uncached(tmp_path):
    ledger = BudgetLedger(tmp_path / "cache.db", limit=20, currency="CNY")
    reservation = ledger.reserve("model", 1000, 100, 2, 8, cached_input_price=0.04)
    settled = ledger.settle(reservation.id, {"prompt_tokens": 1000, "completion_tokens": 10})
    assert settled["actual_cny"] == 0.00208
    assert settled["cache_usage_reported"] is False


def test_cache_schema_migration_does_not_reprice_historical_calls(tmp_path):
    path = tmp_path / "legacy.db"
    ledger = BudgetLedger(path, limit=20, currency="CNY", pricing_basis="peak_uncached_upper_bound")
    first = ledger.reserve("model", 1000, 100, 2, 8)
    ledger.settle(first.id, {"prompt_tokens": 1000, "completion_tokens": 10})
    with sqlite3.connect(path) as db:
        for name in ("cached_input_price", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "cache_usage_reported", "response_id"):
            db.execute(f"ALTER TABLE budget_calls DROP COLUMN {name}")
    migrated = BudgetLedger(path, limit=20, currency="CNY", pricing_basis="peak_uncached_upper_bound")
    assert migrated.summary()["held_cny"] == 0.00208
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT cached_input_price FROM budget_calls WHERE id=?", (first.id,)).fetchone() == (None,)


def test_free_request_ledger_counts_requests_not_invented_prices(tmp_path):
    ledger = BudgetLedger(tmp_path / "free.db", limit=2, currency="FREE",
                          pricing_basis="free_provider_quota", mode="requests")
    first = ledger.reserve("free-model", 1000, 100, units=1)
    second = ledger.reserve("free-model", 1000, 100, units=1)
    assert first.reserved_units == 1 and second.reserved_units == 1
    ledger.settle(first.id, {"prompt_tokens": 120, "completion_tokens": 12})
    ledger.settle(second.id, {"prompt_tokens": 80, "completion_tokens": 8})
    summary = ledger.summary()
    assert summary["mode"] == "requests" and summary["unit"] == "request"
    assert summary["limit"] == 2 and summary["held"] == 2 and summary["remaining"] == 0
    assert summary["held_requests"] == 2 and summary["prompt_tokens"] == 200
    assert summary["total_tokens"] == 220 and summary["token_priced_total"] is None
    assert "held_usd" not in summary
    with pytest.raises(BudgetExceeded):
        ledger.reserve("free-model", 1000, 100, units=1)


def test_free_ledger_rejects_fabricated_prices_and_fractional_requests(tmp_path):
    ledger = BudgetLedger(tmp_path / "free.db", limit=2, currency="FREE",
                          pricing_basis="free_provider_quota", mode="requests")
    with pytest.raises(ValueError, match="fabricated"):
        ledger.reserve("free-model", 1000, 100, 0.01, 0.02, units=1)
    with pytest.raises(ValueError, match="whole"):
        ledger.reserve("free-model", 1000, 100, units=0.5)
    config = {"billing": "free", "free_quota": {"unit": "request"}}
    request = {"model": "free-model", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 10}
    assert request_bounds(request, config, mode="requests")[2:] == (0.0, 0.0)
    with pytest.raises(ValueError, match="billing: free"):
        request_bounds(request, CONFIG, mode="requests")


def test_provider_429_halts_shared_free_ledger(tmp_path):
    path = tmp_path / "free.db"
    ledger = BudgetLedger(path, limit=5, currency="FREE",
                          pricing_basis="free_provider_quota", mode="requests")
    class QuotaError(Exception):
        status_code = 429
    wrapped = _Completions(SimpleNamespace(create=lambda **kw: (_ for _ in ()).throw(
        QuotaError("Free usage limit reached"))), ledger,
        {"free-model": {"billing": "free", "free_quota": {"unit": "request"}}})
    with pytest.raises(QuotaError):
        wrapped.create(model="free-model", messages=[{"role": "user", "content": "hi"}], max_tokens=10)
    summary = ledger.summary()
    assert summary["halted"] and summary["halted_reason"] == "provider_quota_exhausted"
    assert summary["counts"] == {"uncertain": 1}
    with pytest.raises(BudgetExceeded, match="provider_quota_exhausted"):
        BudgetLedger(path, limit=5, currency="FREE", pricing_basis="free_provider_quota",
                     mode="requests").reserve("free-model", 1000, 100, units=1)


def test_free_credit_ledger_uses_configured_credit_cost(tmp_path):
    ledger = BudgetLedger(tmp_path / "credits.db", limit=3, currency="FREE",
                          pricing_basis="free_provider_quota", mode="credits")
    config = {"billing": "free", "free_quota": {"unit": "credit", "credit_per_request": 0.5}}
    raw = SimpleNamespace(create=lambda **kw: SimpleNamespace(model_dump=lambda:
        {"usage": {"prompt_tokens": 10, "completion_tokens": 2}}))
    wrapped = _Completions(raw, ledger, {"free-model": config})
    for _ in range(6):
        wrapped.create(model="free-model", messages=[{"role": "user", "content": "hi"}], max_tokens=10)
    summary = ledger.summary()
    assert summary["held_credits"] == 3 and summary["remaining"] == 0
    with pytest.raises(BudgetExceeded):
        wrapped.create(model="free-model", messages=[{"role": "user", "content": "hi"}], max_tokens=10)


def test_installer_free_mode_reports_zero_cash_without_fake_tariff(monkeypatch, tmp_path):
    from vita_agent.budget import install_budget_guard
    free = {"billing": "free", "free_quota": {"unit": "request"}}
    fake = SimpleNamespace(_get_client=lambda **kw: None, models={"free-model": free})
    monkeypatch.setitem(sys.modules, "vita.utils", SimpleNamespace(llm_utils=fake))
    ledger = install_budget_guard(tmp_path / "free.db", limit=4, currency="FREE",
                                  pricing_basis="free_provider_quota", mode="requests")
    assert fake.BUDGET_MODE == "requests" and fake.BUDGET_UNIT == "request"
    assert fake.get_response_cost({"prompt_tokens": 100, "completion_tokens": 20}, "free-model") == 0.0
    assert ledger.summary()["held_requests"] == 0


def test_ledger_mode_is_immutable_across_phases(tmp_path):
    path = tmp_path / "shared.db"
    BudgetLedger(path, limit=3, currency="FREE", pricing_basis="free_provider_quota", mode="requests")
    with pytest.raises(ValueError, match="immutable"):
        BudgetLedger(path, limit=3, currency="FREE", pricing_basis="free_provider_quota", mode="credits")


def test_official_usage_retains_cache_counts_and_cost_uses_them(monkeypatch, tmp_path):
    from vita_agent.budget import install_budget_guard
    fake = SimpleNamespace(_get_client=lambda **kw: None,
        models={"deepseek-flash": {"cost_1m_token_cny": {"prompt_price": 2, "cached_prompt_price": 0.04, "completion_price": 8}}})
    monkeypatch.setitem(sys.modules, "vita.utils", SimpleNamespace(llm_utils=fake))
    install_budget_guard(tmp_path / "cache.db", limit=20, currency="CNY")
    raw = {"usage": {"prompt_tokens": 1_000_000, "completion_tokens": 1000,
        "prompt_cache_hit_tokens": 990_000, "prompt_cache_miss_tokens": 10_000}}
    preserved = fake.get_response_usage(raw)
    assert preserved["prompt_cache_hit_tokens"] == 990_000
    assert fake.get_response_cost(preserved, "deepseek-flash") == pytest.approx(0.0676)
