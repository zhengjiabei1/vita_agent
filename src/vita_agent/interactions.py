"""Parse user-only benchmark evidence without importing model configuration."""
from datetime import datetime, timezone
import hashlib
import json
from typing import Any
from .models import Evidence

def _timestamp(value: Any) -> float:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Interaction needs a source timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # Benchmark naive dates are interpreted consistently, never using host time.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _mapping(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return {}


def interaction_evidence(interaction: Any, user_id: str) -> list[tuple[Evidence, str]]:
    """Extract only explicitly role-labeled user dialogue, never rubric/behavior.

    Bare ``content`` strings have no reliable speaker and are skipped. Dialogue
    order supplies microsecond ordering within one timestamped interaction.
    Independent equal-time contradictory statements remain conflicts for memory.
    """
    record = _mapping(interaction)
    if not record:
        return []
    domain = record.get("domain", "global")
    if domain not in ("delivery", "instore", "ota", "global"):
        domain = "global"
    messages = record.get("dialogue")
    if messages is None and record.get("type") == "conversation":
        content = record.get("content")
        if isinstance(content, list):
            messages = content
        elif isinstance(content, dict):
            messages = content.get("messages", content.get("dialogue"))
            if messages is None and "role" in content:
                messages = [content]
    if not isinstance(messages, list):
        return []
    timestamp = _timestamp(record.get("timestamp", record.get("date")))
    results = []
    for index, message in enumerate(messages):
        message = _mapping(message)
        text = message.get("content")
        if message.get("role") != "user" or not isinstance(text, str) or not text.strip():
            continue
        observed_at = (_timestamp(message["timestamp"]) if message.get("timestamp")
                       else timestamp + index * 1e-6)
        fingerprint = json.dumps([user_id, domain, observed_at, text], ensure_ascii=False)
        evidence = Evidence(hashlib.sha256(fingerprint.encode()).hexdigest(), user_id, text, observed_at)
        results.append((evidence, domain))
    return results
