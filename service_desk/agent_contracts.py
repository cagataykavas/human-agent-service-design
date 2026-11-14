from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Callable
from typing import Any

from service_desk.errors import AgentOutputViolation, ToolPolicyViolation
from service_desk.models import IssuePriority

_TOKEN = re.compile(r"^[a-z][a-z0-9_]{0,127}$")


def _require_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AgentOutputViolation("model output must be a JSON object")
    if not all(isinstance(key, str) for key in value):
        raise AgentOutputViolation("model output field names must be strings")
    return value


def _require_fields(
    output: dict[str, Any],
    *,
    required: frozenset[str],
) -> None:
    missing = required - output.keys()
    unknown = output.keys() - required
    if missing:
        raise AgentOutputViolation("model output is missing required fields")
    if unknown:
        raise AgentOutputViolation("model output contains unknown fields")


def _confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AgentOutputViolation("confidence must be numeric")
    normalized = float(value)
    if not math.isfinite(normalized) or not 0.0 <= normalized <= 1.0:
        raise AgentOutputViolation("confidence must be finite and in [0, 1]")
    return normalized


def _text(value: Any, *, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise AgentOutputViolation(f"{field} must contain between 1 and {maximum} characters")
    if value != value.strip():
        raise AgentOutputViolation(f"{field} must not have surrounding whitespace")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise AgentOutputViolation(f"{field} must not contain control characters")
    return value


def _token(value: Any, *, field: str) -> str:
    text = _text(value, field=field, maximum=128)
    if _TOKEN.fullmatch(text) is None:
        raise AgentOutputViolation(f"{field} must be a lowercase token")
    return text


def _reason_codes(value: Any) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        raise AgentOutputViolation("reason_codes must contain between 1 and 8 entries")
    normalized = [_token(item, field="reason code") for item in value]
    if len(set(normalized)) != len(normalized):
        raise AgentOutputViolation("reason_codes must be unique")
    return normalized


def _validate_triage(output: dict[str, Any]) -> dict[str, Any]:
    _require_fields(
        output,
        required=frozenset({"category", "priority", "confidence", "reason_codes"}),
    )
    priority = _text(output["priority"], field="priority", maximum=16)
    if priority not in {item.value for item in IssuePriority}:
        raise AgentOutputViolation("priority is not supported")
    return {
        "category": _token(output["category"], field="category"),
        "priority": priority,
        "confidence": _confidence(output["confidence"]),
        "reason_codes": _reason_codes(output["reason_codes"]),
    }


def _validate_summary(output: dict[str, Any]) -> dict[str, Any]:
    _require_fields(output, required=frozenset({"summary", "confidence"}))
    return {
        "summary": _text(output["summary"], field="summary", maximum=240),
        "confidence": _confidence(output["confidence"]),
    }


def _validate_recommendation(output: dict[str, Any]) -> dict[str, Any]:
    _require_fields(
        output,
        required=frozenset({"recommendation", "confidence", "requires_human"}),
    )
    requires_human = output["requires_human"]
    if not isinstance(requires_human, bool):
        raise AgentOutputViolation("requires_human must be a boolean")
    return {
        "recommendation": _token(output["recommendation"], field="recommendation"),
        "confidence": _confidence(output["confidence"]),
        "requires_human": requires_human,
    }


def _validate_retrieval(output: dict[str, Any]) -> dict[str, Any]:
    _require_fields(output, required=frozenset({"query", "confidence"}))
    return {
        "query": _text(output["query"], field="query", maximum=200),
        "confidence": _confidence(output["confidence"]),
    }


_VALIDATORS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "triage": _validate_triage,
    "summarize": _validate_summary,
    "recommend_action": _validate_recommendation,
    "retrieve_knowledge": _validate_retrieval,
}


def validate_model_output(capability: str, value: Any) -> dict[str, Any]:
    """Validate and normalize one model response before persistence or tool use."""

    try:
        validator = _VALIDATORS[capability]
    except KeyError as exc:
        raise AgentOutputViolation("capability has no model-output contract") from exc
    return validator(_require_object(value))


def failure_code(error: Exception) -> str:
    """Map failures to a bounded durable code without persisting exception text."""

    if isinstance(error, AgentOutputViolation):
        return "agent_output_invalid"
    if isinstance(error, ToolPolicyViolation):
        return "tool_policy_violation"
    if isinstance(error, TimeoutError):
        return "dependency_timeout"
    return "agent_execution_failed"
