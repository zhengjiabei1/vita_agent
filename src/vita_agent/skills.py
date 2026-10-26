"""Offline, training-free distillation and frozen skill retrieval.

Trace example (messages/observations/task_lesson are deliberately NEVER copied)::

    {"trace_id": "trace-1", "task_id": "train-1", "split": "train",
     "success": False, "domain": "ota", "steps": [
       {"tool": "book_hotel", "violations": [
         {"code": "budget_exceeded", "message": "Private task detail"}]}]}

Only failed TRAIN traces can produce skills. Step-level guards and a task-level
checklist are distilled with fixed, auditable templates, not an LLM. Raw messages,
observations, arguments, queries, and optional task lessons are never persisted.
Task/trace IDs are retained only in provenance metadata. Domain/tool strings are
mapped into a closed vocabulary so they cannot smuggle personal data into lessons.

Outcome utility = (successes + 1)/(trials + 2) - 0.5 - mean_cost. The first term
is a Beta(1,1) posterior mean, 0.5 is the baseline, and one cost unit carries a
one-point penalty. Use consistent, normalized costs. Test outcomes cannot update
the bank. A frozen JSON artifact contains only active skills and a SHA-256 digest;
the digest detects corruption, not a maliciously re-signed replacement artifact.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any, Iterable


LESSONS = {
    "budget_exceeded": "Compute the full payable total, including fees and quantity, and check it against the current hard budget before committing.",
    "missing_evidence": "Require a current user-scoped evidence reference for every asserted preference; ask for clarification when evidence is absent.",
    "missing_required": "Validate all required tool arguments and resolve missing information before calling the tool.",
    "invalid_dates": "Check date formats, ordering, and supported booking windows before submitting a reservation.",
    "preference_conflict": "Resolve conflicting preferences using explicit current evidence; do not silently relax a hard constraint.",
    "unknown_entity": "Resolve entity identifiers from a current lookup result; never invent an entity identifier.",
    "unavailable": "Check current availability before committing and select an alternative only when it satisfies the same hard constraints.",
    "invalid_quantity": "Validate that quantity is a positive integer within tool limits before calculating totals or committing.",
}
DOMAINS = frozenset({"delivery", "instore", "ota", "general"})
TOOLS = frozenset({"search", "book", "order", "update", "cancel", "lookup", "action"})


@dataclass(frozen=True)
class Provenance:
    task_id: str
    trace_id: str
    split: str = "train"


@dataclass(frozen=True)
class Skill:
    id: str
    level: str
    domain: str
    tool: str
    lesson: str
    failure_codes: tuple[str, ...]
    provenance: tuple[Provenance, ...]
    trials: int = 0
    successes: int = 0
    total_cost: float = 0.0
    utility: float = 0.0
    active: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _domain(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("domain must be a string")
    aliases = {"hotel": "ota", "hotels": "ota", "flight": "ota", "travel": "ota",
               "restaurant": "instore", "restaurants": "instore", "food_delivery": "delivery"}
    return value if value in DOMAINS else aliases.get(value, "general")


def _tool(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Each step needs a nonempty tool name")
    # Only a known first token influences the output; the original is discarded.
    verb = re.split(r"[._:/\s-]", value.casefold())[0]
    return {"search": "search", "find": "search", "book": "book", "reserve": "book",
            "order": "order", "purchase": "order", "update": "update", "modify": "update",
            "cancel": "cancel", "get": "lookup", "lookup": "lookup"}.get(verb, "action")


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError(f"{label} must be a nonempty identifier of at most 256 characters")
    return value


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _skill_id(level: str, domain: str, tool: str, codes: tuple[str, ...]) -> str:
    return "skill-" + hashlib.sha256(_canonical([level, domain, tool, codes])).hexdigest()[:24]


def _lesson(level: str, tool: str, codes: tuple[str, ...]) -> str:
    prefix = "Before completing the task: " if level == "task" else f"Before the {tool} step: "
    return prefix + " ".join(LESSONS[code] for code in codes)


def _utility(trials: int, successes: int, total_cost: float) -> float:
    return (successes + 1) / (trials + 2) - 0.5 - (total_cost / trials if trials else 0.0)


def _retrieve(skills: Iterable[Skill], query: str, domain: str, limit: int) -> list[Skill]:
    if not isinstance(query, str):
        raise ValueError("query must be a string")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise ValueError("limit must be a nonnegative integer")
    selected_domain = _domain(domain)
    query_tokens = set(re.findall(r"\w+", query.casefold()))

    def score(skill: Skill) -> tuple[float, str]:
        tokens = set(re.findall(r"\w+", (skill.lesson + " " + " ".join(skill.failure_codes)).casefold()))
        overlap = len(query_tokens & tokens) / max(1, len(query_tokens))
        return (-(overlap + 0.1 * skill.utility + 0.05 * (skill.domain == selected_domain)), skill.id)

    return sorted((s for s in skills if s.active and s.domain in (selected_domain, "general")), key=score)[:limit]


class SkillBank:
    """Mutable offline SQLite bank. Do not attach this object to test execution."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.executescript("""
            CREATE TABLE IF NOT EXISTS skills (
                id TEXT PRIMARY KEY, level TEXT NOT NULL, domain TEXT NOT NULL,
                tool TEXT NOT NULL, lesson TEXT NOT NULL, failure_codes TEXT NOT NULL,
                trials INTEGER NOT NULL DEFAULT 0, successes INTEGER NOT NULL DEFAULT 0,
                total_cost REAL NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS provenance (
                skill_id TEXT NOT NULL REFERENCES skills(id), task_id TEXT NOT NULL,
                trace_id TEXT NOT NULL,
                PRIMARY KEY (skill_id, task_id, trace_id)
            );
        """)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> SkillBank:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _skill(self, row: sqlite3.Row) -> Skill:
        provenance = tuple(Provenance(p["task_id"], p["trace_id"]) for p in self._connection.execute(
            "SELECT task_id, trace_id FROM provenance WHERE skill_id = ? ORDER BY task_id, trace_id", (row["id"],)))
        return Skill(row["id"], row["level"], row["domain"], row["tool"], row["lesson"],
                     tuple(json.loads(row["failure_codes"])), provenance, row["trials"], row["successes"],
                     row["total_cost"], _utility(row["trials"], row["successes"], row["total_cost"]), bool(row["active"]))

    def list_skills(self, include_retired: bool = False) -> list[Skill]:
        query = "SELECT * FROM skills" + ("" if include_retired else " WHERE active = 1") + " ORDER BY id"
        return [self._skill(row) for row in self._connection.execute(query)]

    def distill(self, trace: dict[str, Any]) -> list[Skill]:
        """Distill known failure checks; ignore free text entirely and deduplicate.

        A trace must identify itself as failed training data. Unknown failure codes
        produce no lesson. Repeated distillation does not reset utility or revive
        retired skills. Caller-supplied split metadata is an explicit trust boundary.
        """
        if not isinstance(trace, dict) or trace.get("split") != "train":
            raise ValueError("Distillation is offline and accepts only train traces")
        if trace.get("success") is not False:
            raise ValueError("Distillation requires a failed trace (success=false)")
        trace_id = _identifier(trace.get("trace_id"), "trace_id")
        task_id = _identifier(trace.get("task_id"), "task_id")
        domain = _domain(trace.get("domain"))
        steps = trace.get("steps")
        if not isinstance(steps, list):
            raise ValueError("steps must be a list")
        specs: set[tuple[str, str, tuple[str, ...]]] = set()
        all_codes: set[str] = set()
        # Validate every step before writing, so a malformed later step is atomic.
        for step in steps:
            if not isinstance(step, dict):
                raise ValueError("Each step must be an object")
            tool = _tool(step.get("tool"))
            violations = step.get("violations", [])
            if not isinstance(violations, list):
                raise ValueError("violations must be a list")
            codes: set[str] = set()
            for violation in violations:
                if not isinstance(violation, dict) or not isinstance(violation.get("code"), str):
                    raise ValueError("Each violation must have a string code")
                if violation["code"] in LESSONS:
                    codes.add(violation["code"])
            if codes:
                specs.add(("step", tool, tuple(sorted(codes))))
                all_codes.update(codes)
        if all_codes:
            specs.add(("task", "action", tuple(sorted(all_codes))))
        ids = []
        with self._connection:
            for level, tool, codes in sorted(specs):
                skill_id = _skill_id(level, domain, tool, codes)
                self._connection.execute(
                    "INSERT OR IGNORE INTO skills(id, level, domain, tool, lesson, failure_codes) VALUES(?,?,?,?,?,?)",
                    (skill_id, level, domain, tool, _lesson(level, tool, codes), json.dumps(codes)))
                self._connection.execute("INSERT OR IGNORE INTO provenance(skill_id, task_id, trace_id) VALUES(?,?,?)",
                                         (skill_id, task_id, trace_id))
                ids.append(skill_id)
        return [self._skill(self._connection.execute("SELECT * FROM skills WHERE id = ?", (skill_id,)).fetchone())
                for skill_id in ids]

    def retrieve(self, query: str, domain: str, limit: int = 5) -> list[Skill]:
        return _retrieve(self.list_skills(), query, domain, limit)

    def record_outcome(self, skill_id: str, success: bool, cost: float = 0, split: str = "dev") -> None:
        """Record only train/dev trials, with a consistent normalized cost unit."""
        if split not in ("train", "dev"):
            raise ValueError("Only offline train/dev outcomes may update skill utility; test is frozen")
        if not isinstance(success, bool):
            raise ValueError("success must be boolean")
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0:
            raise ValueError("cost must be a finite nonnegative number")
        with self._connection:
            row = self._connection.execute("SELECT active, total_cost FROM skills WHERE id = ?", (skill_id,)).fetchone()
            if row is None:
                raise KeyError(skill_id)
            if not row["active"]:
                raise ValueError("Cannot update a retired skill")
            if not math.isfinite(row["total_cost"] + cost):
                raise ValueError("Cumulative cost overflow")
            self._connection.execute("UPDATE skills SET trials=trials+1, successes=successes+?, total_cost=total_cost+? WHERE id=?",
                                     (int(success), cost, skill_id))

    def retire(self, min_trials: int = 3, min_utility: float = 0.0) -> list[str]:
        """Deactivate skills with enough offline trials and utility below cutoff."""
        if not isinstance(min_trials, int) or isinstance(min_trials, bool) or min_trials < 1:
            raise ValueError("min_trials must be a positive integer")
        if not isinstance(min_utility, (int, float)) or not math.isfinite(min_utility):
            raise ValueError("min_utility must be finite")
        retired = [s.id for s in self.list_skills() if s.trials >= min_trials and s.utility < min_utility]
        with self._connection:
            self._connection.executemany("UPDATE skills SET active=0 WHERE id=?", [(s,) for s in retired])
        return retired

    def freeze(self, path: str | Path, excluded_task_ids: Iterable[str] = ()) -> str:
        """Write a new immutable snapshot; return its SHA-256 content digest.

        Fails if any bank provenance intersects held-out task IDs, even for retired
        skills. Existing files are never overwritten. Freeze creates a separate
        immutable object; it does not prevent further offline work on this bank.
        """
        if isinstance(excluded_task_ids, str):
            raise ValueError("excluded_task_ids must be an iterable of identifiers")
        excluded = {_identifier(v, "excluded task id") for v in excluded_task_ids}
        all_skills = self.list_skills(include_retired=True)
        seen_tasks = {p.task_id for skill in all_skills for p in skill.provenance}
        if seen_tasks & excluded:
            raise ValueError("Held-out task provenance intersects the offline skill bank")
        payload = {"format": "vita-skill-bank", "version": 1, "source_split": "train",
                   "utility_formula": "(successes+1)/(trials+2)-0.5-mean_cost",
                   "skills": [s.to_dict() for s in all_skills if s.active]}
        digest = hashlib.sha256(_canonical(payload)).hexdigest()
        document = {"payload": payload, "sha256": digest}
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as stream:
                temporary = stream.name
                stream.write(_canonical(document) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o444)
            os.link(temporary, path)  # Atomic creation; refuses replacement.
        finally:
            if temporary is not None:
                os.unlink(temporary)
        return digest


def _parse_skill(value: Any) -> Skill:
    expected = {"id", "level", "domain", "tool", "lesson", "failure_codes", "provenance",
                "trials", "successes", "total_cost", "utility", "active"}
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("Invalid frozen skill schema")
    level, domain, tool = value["level"], value["domain"], value["tool"]
    if level not in ("task", "step") or domain not in DOMAINS or tool not in TOOLS:
        raise ValueError("Invalid frozen skill category")
    if level == "task" and tool != "action":
        raise ValueError("Task-level skill must use the action category")
    codes = value["failure_codes"]
    if (not isinstance(codes, list) or not codes or not all(isinstance(c, str) and c in LESSONS for c in codes)
            or codes != sorted(set(codes))):
        raise ValueError("Invalid frozen failure codes")
    codes = tuple(codes)
    if value["id"] != _skill_id(level, domain, tool, codes) or value["lesson"] != _lesson(level, tool, codes):
        raise ValueError("Frozen skill content does not match safe distillation templates")
    provenance = value["provenance"]
    if not isinstance(provenance, list) or not provenance:
        raise ValueError("Frozen skill requires training provenance")
    parsed = []
    for p in provenance:
        if not isinstance(p, dict) or set(p) != {"task_id", "trace_id", "split"} or p["split"] != "train":
            raise ValueError("Invalid or non-training provenance")
        parsed.append(Provenance(_identifier(p["task_id"], "task_id"), _identifier(p["trace_id"], "trace_id")))
    if len(set(parsed)) != len(parsed):
        raise ValueError("Duplicate provenance")
    trials, successes = value["trials"], value["successes"]
    if (type(trials) is not int or type(successes) is not int or trials < 0 or not 0 <= successes <= trials):
        raise ValueError("Invalid frozen outcome counts")
    cost, utility = value["total_cost"], value["utility"]
    if (type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0
            or (trials == 0 and cost != 0) or type(utility) not in (int, float) or not math.isfinite(utility)):
        raise ValueError("Invalid frozen utility values")
    if not math.isclose(utility, _utility(trials, successes, cost), abs_tol=1e-12) or value["active"] is not True:
        raise ValueError("Frozen utility mismatch or inactive skill")
    return Skill(value["id"], level, domain, tool, value["lesson"], codes, tuple(parsed), trials, successes, cost, utility, True)


@dataclass(frozen=True)
class FrozenSkillBank:
    """Read-only retrieval artifact, with no path back to the mutable SQLite bank."""

    _skills: tuple[Skill, ...]
    digest: str

    @classmethod
    def load(cls, path: str | Path) -> FrozenSkillBank:
        def reject_constant(value: str) -> None:
            raise ValueError(f"Invalid JSON numeric constant: {value}")

        with Path(path).open(encoding="utf-8") as stream:
            document = json.load(stream, parse_constant=reject_constant)
        if not isinstance(document, dict) or set(document) != {"payload", "sha256"}:
            raise ValueError("Invalid frozen bank envelope")
        payload = document["payload"]
        if not isinstance(document["sha256"], str) or hashlib.sha256(_canonical(payload)).hexdigest() != document["sha256"]:
            raise ValueError("Frozen bank SHA-256 integrity check failed")
        keys = {"format", "version", "source_split", "utility_formula", "skills"}
        if (not isinstance(payload, dict) or set(payload) != keys or payload["format"] != "vita-skill-bank"
                or type(payload["version"]) is not int or payload["version"] != 1 or payload["source_split"] != "train"
                or payload["utility_formula"] != "(successes+1)/(trials+2)-0.5-mean_cost" or not isinstance(payload["skills"], list)):
            raise ValueError("Unsupported frozen bank schema")
        try:
            skills = tuple(_parse_skill(value) for value in payload["skills"])
        except (TypeError, OverflowError) as exc:
            raise ValueError("Malformed frozen skill schema") from exc
        if len({s.id for s in skills}) != len(skills):
            raise ValueError("Duplicate frozen skill IDs")
        return cls(skills, document["sha256"])

    def list_skills(self, include_retired: bool = False) -> list[Skill]:
        return list(self._skills)

    def retrieve(self, query: str, domain: str, limit: int = 5) -> list[Skill]:
        return _retrieve(self._skills, query, domain, limit)

    def distill(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Frozen skill banks cannot distill traces")

    def record_outcome(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Frozen skill banks cannot learn from outcomes")

    def retire(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Frozen skill banks cannot retire skills")

    def freeze(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Frozen skill banks cannot mutate or replace snapshots")
