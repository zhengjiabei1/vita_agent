"""Shared, JSON-compatible records for grounded decisions and audit trails."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Evidence:
    id: str
    user_id: str
    text: str
    timestamp: float
    source: str = "user"


@dataclass(frozen=True)
class Preference:
    id: str
    user_id: str
    domain: str
    slot: str
    value: Any
    strength: str  # hard | soft
    operator: str  # eq | lte | gte | excludes | includes
    evidence_id: str
    quote: str
    timestamp: float
    status: str = "active"
    supersedes: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Action:
    tool: str
    arguments: dict[str, Any]
    idempotency_key: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Violation:
    code: str
    message: str
    slot: str | None = None
    evidence_id: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Proposal:
    action: Action
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0


@dataclass
class RunResult:
    status: str  # executed | blocked | failed
    action: Action | None
    observation: Any
    attempts: list[dict] = field(default_factory=list)
    cost: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0

    def to_dict(self) -> dict:
        return asdict(self)
