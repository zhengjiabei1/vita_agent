"""Draft/check/regenerate before a single, journaled side-effecting operation.

Planner output is untrusted. Facts come exclusively from a caller-supplied
resolver backed by observations/catalog state. Backend idempotency is still
required for real services: a process crash after commit is recorded as uncertain
and is NOT retried automatically.

Bind resolver/executor instances to the current authenticated user. The executor
contract returns a JSON-serializable successful result and raises on any service
failure; ``executed`` means that contract completed, not an independent rubric
judgment. For APIs returning error objects, translate those to exceptions in the
adapter. Real backends must atomically enforce availability and constraints too.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import sqlite3
from typing import Callable

from .guards import ConstraintChecker
from .models import Action, Proposal, RunResult, Violation


class ExecutionJournal:
    def __init__(self, path: str = ":memory:"):
        self.db = sqlite3.connect(path, timeout=10)
        self.db.execute("CREATE TABLE IF NOT EXISTS executions (user_id TEXT, key TEXT, digest TEXT, state TEXT, result TEXT, PRIMARY KEY(user_id,key))")

    def claim(self, user_id: str, action: Action) -> tuple[str, object]:
        payload = json.dumps({"tool": action.tool, "arguments": action.arguments}, sort_keys=True, allow_nan=False)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self.db:
            try:
                self.db.execute("INSERT INTO executions VALUES (?,?,?,'pending',NULL)", (user_id, action.idempotency_key, digest))
                return "new", None
            except sqlite3.IntegrityError:
                previous = self.db.execute("SELECT digest,state,result FROM executions WHERE user_id=? AND key=?", (user_id, action.idempotency_key)).fetchone()
                if previous[0] != digest:
                    return "conflict", None
                return previous[1], json.loads(previous[2]) if previous[2] else None

    def finish(self, user_id: str, key: str, state: str, result: object) -> None:
        serialized = json.dumps(result, ensure_ascii=False, allow_nan=False)
        with self.db:
            self.db.execute("UPDATE executions SET state=?, result=? WHERE user_id=? AND key=?", (state, serialized, user_id, key))

    def close(self):
        self.db.close()


class GuardedHarness:
    def __init__(self, memory, resolver: Callable[[Action], dict], executor: Callable[[Action], object],
                 *, checker=None, skill_bank=None, journal=None, max_regenerations: int = 2):
        if isinstance(max_regenerations, bool) or not isinstance(max_regenerations, int) or max_regenerations < 0:
            raise ValueError("max_regenerations must be a nonnegative integer")
        self.memory, self.resolver, self.executor = memory, resolver, executor
        self.checker = checker or ConstraintChecker()
        self.skill_bank = skill_bank
        self.journal = journal if journal is not None else ExecutionJournal()
        self.max_regenerations = max_regenerations

    def run(self, user_id: str, domain: str, request: str, planner: Callable) -> RunResult:
        if not user_id or not domain or not request.strip():
            raise ValueError("user_id, domain and request are required")
        preferences = self.memory.active(user_id, domain=domain, strength="hard")
        skills = self.skill_bank.retrieve(request, domain=domain, limit=5) if self.skill_bank else []
        context = {
            "request": request, "domain": domain,
            "memory": self.memory.render(user_id, query=request, domain=domain),
            "constraints": [p.to_dict() for p in preferences],
            "skills": [s.to_dict() for s in skills],
        }
        result = RunResult("blocked", None, None)
        feedback: list[dict] = []
        for attempt in range(self.max_regenerations + 1):
            accounted = False
            try:
                proposal = planner(deepcopy(context), deepcopy(feedback))
                if not isinstance(proposal, Proposal):
                    raise ValueError("Planner must return a Proposal")
                if isinstance(proposal.cost, bool) or not isinstance(proposal.cost, (int, float)) or not math.isfinite(proposal.cost) or proposal.cost < 0:
                    raise ValueError("Planner cost must be finite and nonnegative")
                if any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in (proposal.input_tokens, proposal.output_tokens)):
                    raise ValueError("Token counts must be nonnegative integers")
                result.cost += proposal.cost
                result.input_tokens += proposal.input_tokens
                result.output_tokens += proposal.output_tokens
                accounted = True
                action = deepcopy(proposal.action)
                if not isinstance(action, Action) or not action.idempotency_key or not isinstance(action.arguments, dict):
                    raise ValueError("Action and idempotency key are required")
                json.dumps(action.to_dict(), allow_nan=False)
            except Exception as exc:
                result.status = "failed"
                result.observation = {"error": "planner_error", "type": type(exc).__name__}
                if accounted:
                    result.attempts.append({"attempt": attempt + 1, "action": None,
                        "violations": [{"code": "invalid_draft", "message": "Malformed action from planner"}],
                        "input_tokens": proposal.input_tokens, "output_tokens": proposal.output_tokens, "cost": proposal.cost})
                return result
            result.action = action
            try:
                facts = self.resolver(deepcopy(action))
                violations = self.checker.check(action, preferences, facts)
                spec = self.checker.tool_specs.get(action.tool)
                if spec and spec.domain and spec.domain != domain:
                    violations.append(Violation("domain_mismatch", "Proposed tool does not belong to this request's domain"))
            except Exception as exc:
                violations = [Violation("missing_evidence", f"Trusted resolver could not verify action ({type(exc).__name__})")]
            feedback = [v.to_dict() for v in violations]
            result.attempts.append({"attempt": attempt + 1, "action": action.to_dict(), "violations": feedback,
                                    "input_tokens": proposal.input_tokens, "output_tokens": proposal.output_tokens,
                                    "cost": proposal.cost})
            if violations:
                continue
            try:
                state, cached = self.journal.claim(user_id, action)
            except Exception as exc:
                result.status = "failed"
                result.observation = {"error": "journal_unavailable", "type": type(exc).__name__}
                return result
            if state == "done":
                result.status, result.observation = "executed", cached
                result.attempts[-1]["cached_execution"] = True
                return result
            if state != "new":
                result.status = "blocked"
                result.observation = {"error": "idempotency_conflict" if state == "conflict" else "execution_uncertain"}
                return result
            try:
                observation = self.executor(deepcopy(action))
                self.journal.finish(user_id, action.idempotency_key, "done", observation)
            except Exception as exc:
                result.status = "failed"
                result.observation = {"error": "execution_uncertain", "type": type(exc).__name__, "retry": False}
                try:
                    self.journal.finish(user_id, action.idempotency_key, "uncertain", result.observation)
                except Exception:
                    # The persisted claim remains pending; never retry a possible commit.
                    pass
                return result
            result.status, result.observation = "executed", observation
            return result
        result.observation = {"error": "regeneration_exhausted", "violations": feedback}
        return result
