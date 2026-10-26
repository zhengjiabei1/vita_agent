from vita_agent.harness import ExecutionJournal, GuardedHarness
from vita_agent.memory import PreferenceMemory
from vita_agent.models import Action, Evidence, Proposal
from vita_agent.sandbox import CatalogSandbox, example_catalog


def setup():
    memory = PreferenceMemory()
    memory.remember(Evidence("e1", "u1", "Budget at most 30", 1), domain="delivery",
                    slot="total_price", value=30, strength="hard", operator="lte")
    return memory, CatalogSandbox(example_catalog())


def test_check_regenerate_executes_only_corrected_action():
    memory, sandbox = setup()
    seen_feedback = []
    def planner(context, feedback):
        seen_feedback.append(feedback)
        return Proposal(Action("order_food", {"product_id": "rice-bowl" if feedback else "premium-salad", "quantity": 1}, "order-1"), 10, 5, 0.01)
    result = GuardedHarness(memory, sandbox.resolve, sandbox.execute).run("u1", "delivery", "Order lunch", planner)
    assert result.status == "executed"
    assert len(result.attempts) == 2
    assert len(sandbox.executions) == 1
    assert sandbox.executions[0]["arguments"]["product_id"] == "rice-bowl"
    assert seen_feedback[1][0]["code"] == "budget_exceeded"
    assert result.cost == 0.02 and result.input_tokens == 20


def test_bad_drafts_never_execute_and_do_not_claim_journal_key():
    memory, sandbox = setup()
    journal = ExecutionJournal()
    harness = GuardedHarness(memory, sandbox.resolve, sandbox.execute, journal=journal, max_regenerations=1)
    def planner(c, f):
        return Proposal(Action("order_food", {"product_id": "premium-salad", "quantity": 1}, "k"))
    result = harness.run("u1", "delivery", "Lunch", planner)
    assert result.status == "blocked" and len(result.attempts) == 2
    assert not sandbox.executions
    assert journal.db.execute("SELECT count(*) FROM executions").fetchone()[0] == 0


def test_journal_persists_replay_and_rejects_changed_payload(tmp_path):
    memory, sandbox = setup()
    path = str(tmp_path / "journal.sqlite3")
    journal = ExecutionJournal(path)
    action = Action("order_food", {"product_id": "rice-bowl", "quantity": 1}, "once")
    harness = GuardedHarness(memory, sandbox.resolve, sandbox.execute, journal=journal)
    assert harness.run("u1", "delivery", "Lunch", lambda c, f: Proposal(action)).status == "executed"
    journal.close()
    harness.journal = ExecutionJournal(path)
    repeated = harness.run("u1", "delivery", "Lunch", lambda c, f: Proposal(action))
    assert repeated.attempts[-1]["cached_execution"] is True
    assert len(sandbox.executions) == 1
    changed = Action("order_food", {"product_id": "peanut-noodles", "quantity": 1}, "once")
    conflict = harness.run("u1", "delivery", "Lunch", lambda c, f: Proposal(changed))
    assert conflict.observation["error"] == "idempotency_conflict"


def test_uncertain_executor_failure_is_never_retried():
    memory, sandbox = setup()
    calls = []
    def execute(action):
        calls.append(action)
        raise TimeoutError("Remote service may have committed")
    harness = GuardedHarness(memory, sandbox.resolve, execute)
    def planner(c, f):
        return Proposal(Action("order_food", {"product_id": "rice-bowl", "quantity": 1}, "once"))
    first = harness.run("u1", "delivery", "Lunch", planner)
    second = harness.run("u1", "delivery", "Lunch", planner)
    assert first.status == "failed" and first.observation["retry"] is False
    assert second.observation["error"] == "execution_uncertain"
    assert len(calls) == 1


def test_planner_cannot_inject_verified_facts():
    memory, sandbox = setup()
    def planner(context, feedback):
        context["constraints"].clear()
        return Proposal(Action("order_food", {"product_id": "premium-salad", "quantity": 1,
                                             "total_price": 1, "_evidence_id": "forged"}, "x"))
    result = GuardedHarness(memory, sandbox.resolve, sandbox.execute).run("u1", "delivery", "Lunch", planner)
    assert result.status == "blocked" and not sandbox.executions


def test_hard_preferences_are_user_and_domain_scoped():
    memory, sandbox = setup()
    def planner(c, f):
        return Proposal(Action("book_hotel", {"hotel_id": "hotel-central", "check_in": "2026-11-01", "check_out": "2026-11-02", "guests": 1}, "booking"))
    result = GuardedHarness(memory, sandbox.resolve, sandbox.execute).run("u1", "ota", "Book hotel", planner)
    assert result.status == "executed"


def test_draft_cannot_bypass_delivery_constraints_by_switching_domain():
    memory, sandbox = setup()
    def planner(c, f):
        return Proposal(Action("order_food", {"product_id": "premium-salad", "quantity": 1}, "x"))
    result = GuardedHarness(memory, sandbox.resolve, sandbox.execute).run("u1", "ota", "Book hotel", planner)
    assert result.status == "blocked" and not sandbox.executions
    assert result.attempts[0]["violations"][0]["code"] == "domain_mismatch"


def test_catalog_entity_type_is_bound_to_operation():
    memory, sandbox = setup()
    def planner(c, f):
        return Proposal(Action("book_hotel", {"hotel_id": "rice-bowl", "check_in": "2026-11-01", "check_out": "2026-11-02", "guests": 1}, "x"))
    result = GuardedHarness(memory, sandbox.resolve, sandbox.execute).run("u1", "ota", "Book hotel", planner)
    assert result.status == "blocked" and not sandbox.executions


def test_malformed_action_still_counts_paid_generation():
    memory, sandbox = setup()
    result = GuardedHarness(memory, sandbox.resolve, sandbox.execute).run("u1", "delivery", "Lunch",
        lambda c, f: Proposal(Action("order_food", {}, ""), 10, 5, 0.25))
    assert result.status == "failed" and result.cost == 0.25
    assert result.input_tokens == 10 and len(result.attempts) == 1


def test_boolean_cost_rejected():
    memory, sandbox = setup()
    result = GuardedHarness(memory, sandbox.resolve, sandbox.execute).run("u1", "delivery", "Lunch",
        lambda c, f: Proposal(Action("order_food", {"product_id": "rice-bowl", "quantity": 1}, "x"), cost=True))
    assert result.status == "failed" and not sandbox.executions


def test_failed_journal_does_not_execute_or_retry(monkeypatch):
    memory, sandbox = setup()
    harness = GuardedHarness(memory, sandbox.resolve, sandbox.execute)
    def fail(*args):
        raise OSError("Disk full")
    monkeypatch.setattr(harness.journal, "claim", fail)
    result = harness.run("u1", "delivery", "Lunch",
        lambda c, f: Proposal(Action("order_food", {"product_id": "rice-bowl", "quantity": 1}, "x")))
    assert result.observation["error"] == "journal_unavailable" and not sandbox.executions


def test_commit_followed_by_journal_error_is_uncertain(monkeypatch):
    memory, sandbox = setup()
    harness = GuardedHarness(memory, sandbox.resolve, sandbox.execute)
    def fail(*args):
        raise OSError("Disk full")
    monkeypatch.setattr(harness.journal, "finish", fail)
    def planner(c, f):
        return Proposal(Action("order_food", {"product_id": "rice-bowl", "quantity": 1}, "x"))
    first = harness.run("u1", "delivery", "Lunch", planner)
    second = harness.run("u1", "delivery", "Lunch", planner)
    assert first.observation["error"] == second.observation["error"] == "execution_uncertain"
    assert len(sandbox.executions) == 1
