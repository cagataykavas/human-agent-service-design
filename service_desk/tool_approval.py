from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from service_desk.errors import ToolPolicyViolation
from service_desk.models import ToolRisk


def request_digest(
    *,
    job_id: str,
    tool_name: str,
    risk: ToolRisk,
    arguments: dict[str, Any],
    requested_by: str,
) -> str:
    """Return a canonical identity for the exact tool request a human reviews."""
    payload = {
        "arguments": arguments,
        "job_id": job_id,
        "requested_by": requested_by,
        "risk": risk.value,
        "tool_name": tool_name,
    }
    try:
        encoded = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ToolPolicyViolation("tool arguments must be finite JSON values") from exc
    return hashlib.sha256(encoded).hexdigest()


def digests_match(left: str, right: str) -> bool:
    try:
        return hmac.compare_digest(left.encode("ascii"), right.encode("ascii"))
    except (AttributeError, UnicodeError):
        return False


def write_target(arguments: dict[str, Any]) -> tuple[str, int]:
    """Extract the optimistic-concurrency target required by every write tool."""
    issue_id = arguments.get("issue_id")
    expected_version = arguments.get("expected_version")
    if not isinstance(issue_id, str) or not issue_id.strip():
        raise ToolPolicyViolation("write tool arguments require a non-empty issue_id")
    if (
        not isinstance(expected_version, int)
        or isinstance(expected_version, bool)
        or expected_version < 1
    ):
        raise ToolPolicyViolation("write tool arguments require a positive expected_version")
    return issue_id, expected_version
