from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

MAX_DOCUMENT_BYTES = 128 * 1024
MAX_STRING_LENGTH = 512
MAX_COLLECTION_ITEMS = 128
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class HandoffFormatError(ValueError):
    """The handoff cannot be trusted enough to evaluate."""


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    evidence_id: str
    sha256: str
    producer_call_id: str


@dataclass(frozen=True, slots=True)
class HandoffArtifact:
    schema_version: int
    handoff_id: str
    issue_id: str
    issue_version: int
    job_id: str
    capability: str
    job_state: str
    reason_code: str
    created_at: datetime
    expires_at: datetime
    policy_id: str
    trace_digest: str
    recommended_actions: tuple[str, ...]
    evidence: tuple[EvidenceReference, ...]
    unresolved_question_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TrustedEvidence:
    sha256: str
    producer_call_id: str


@dataclass(frozen=True, slots=True)
class HandoffContext:
    issue_id: str
    issue_version: int
    job_id: str
    capability: str
    job_state: str
    policy_id: str
    trace_digest: str
    allowed_actions: frozenset[str]
    trusted_evidence: Mapping[str, TrustedEvidence]


@dataclass(frozen=True, slots=True)
class HandoffPolicy:
    allowed_reasons: frozenset[str]
    allowed_job_states: frozenset[str] = frozenset({"waiting_for_approval", "failed", "running"})
    require_evidence: bool = True
    max_lifetime_seconds: int = 3600
    max_future_skew_seconds: int = 30
    max_actions: int = 16
    max_evidence: int = 32
    max_questions: int = 16

    def __post_init__(self) -> None:
        if not self.allowed_reasons:
            raise ValueError("allowed_reasons must not be empty")
        if not 1 <= self.max_lifetime_seconds <= 86_400:
            raise ValueError("max_lifetime_seconds must be between 1 and 86400")
        if not 0 <= self.max_future_skew_seconds <= 300:
            raise ValueError("max_future_skew_seconds must be between 0 and 300")
        for name, value, limit in (
            ("max_actions", self.max_actions, 64),
            ("max_evidence", self.max_evidence, 128),
            ("max_questions", self.max_questions, 64),
        ):
            if not 1 <= value <= limit:
                raise ValueError(f"{name} must be between 1 and {limit}")


@dataclass(frozen=True, slots=True)
class HandoffDecision:
    accepted: bool
    finding_codes: tuple[str, ...]
    evidence_id: str
    action_count: int
    evidence_count: int
    unresolved_question_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HandoffFormatError("duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise HandoffFormatError(f"non-finite number {value!r} is not allowed")


def _load_json(raw: bytes) -> Any:
    if not raw or len(raw) > MAX_DOCUMENT_BYTES:
        raise HandoffFormatError("document size is outside the accepted range")
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HandoffFormatError("document is not valid UTF-8 JSON") from exc


def _expect_object(value: Any, *, fields: frozenset[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HandoffFormatError(f"{name} must be an object")
    unknown = set(value) - fields
    missing = fields - set(value)
    if unknown or missing:
        raise HandoffFormatError(f"{name} has missing or unknown fields")
    return value


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or len(value) > MAX_STRING_LENGTH or not _ID.fullmatch(value):
        raise HandoffFormatError(f"{name} is invalid")
    return value


def _digest(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise HandoffFormatError(f"{name} must be a canonical SHA-256 digest")
    return value


def _timestamp(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise HandoffFormatError(f"{name} is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HandoffFormatError(f"{name} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HandoffFormatError(f"{name} must include a UTC offset")
    return parsed.astimezone(UTC)


def _identifier_list(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > MAX_COLLECTION_ITEMS:
        raise HandoffFormatError(f"{name} must be a bounded list")
    result = tuple(_identifier(item, name) for item in value)
    if len(set(result)) != len(result):
        raise HandoffFormatError(f"{name} must not contain duplicates")
    return result


_ARTIFACT_FIELDS = frozenset(
    {
        "schema_version",
        "handoff_id",
        "issue_id",
        "issue_version",
        "job_id",
        "capability",
        "job_state",
        "reason_code",
        "created_at",
        "expires_at",
        "policy_id",
        "trace_digest",
        "recommended_actions",
        "evidence",
        "unresolved_question_codes",
    }
)
_EVIDENCE_FIELDS = frozenset({"evidence_id", "sha256", "producer_call_id"})


def parse_handoff(raw: bytes) -> HandoffArtifact:
    document = _expect_object(_load_json(raw), fields=_ARTIFACT_FIELDS, name="handoff")
    if document["schema_version"] != 1:
        raise HandoffFormatError("unsupported schema_version")
    version = document["issue_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise HandoffFormatError("issue_version must be a positive integer")
    raw_evidence = document["evidence"]
    if not isinstance(raw_evidence, list) or len(raw_evidence) > MAX_COLLECTION_ITEMS:
        raise HandoffFormatError("evidence must be a bounded list")
    evidence: list[EvidenceReference] = []
    for item in raw_evidence:
        entry = _expect_object(item, fields=_EVIDENCE_FIELDS, name="evidence entry")
        evidence.append(
            EvidenceReference(
                evidence_id=_identifier(entry["evidence_id"], "evidence_id"),
                sha256=_digest(entry["sha256"], "evidence sha256"),
                producer_call_id=_identifier(entry["producer_call_id"], "producer_call_id"),
            )
        )
    if len({item.evidence_id for item in evidence}) != len(evidence):
        raise HandoffFormatError("evidence_id must be unique")
    artifact = HandoffArtifact(
        schema_version=1,
        handoff_id=_identifier(document["handoff_id"], "handoff_id"),
        issue_id=_identifier(document["issue_id"], "issue_id"),
        issue_version=version,
        job_id=_identifier(document["job_id"], "job_id"),
        capability=_identifier(document["capability"], "capability"),
        job_state=_identifier(document["job_state"], "job_state"),
        reason_code=_identifier(document["reason_code"], "reason_code"),
        created_at=_timestamp(document["created_at"], "created_at"),
        expires_at=_timestamp(document["expires_at"], "expires_at"),
        policy_id=_identifier(document["policy_id"], "policy_id"),
        trace_digest=_digest(document["trace_digest"], "trace_digest"),
        recommended_actions=_identifier_list(document["recommended_actions"], "action"),
        evidence=tuple(evidence),
        unresolved_question_codes=_identifier_list(
            document["unresolved_question_codes"], "unresolved question"
        ),
    )
    if artifact.expires_at <= artifact.created_at:
        raise HandoffFormatError("expires_at must be after created_at")
    return artifact


def _canonical_artifact(artifact: HandoffArtifact) -> bytes:
    payload = {
        "schema_version": artifact.schema_version,
        "handoff_id": artifact.handoff_id,
        "issue_id": artifact.issue_id,
        "issue_version": artifact.issue_version,
        "job_id": artifact.job_id,
        "capability": artifact.capability,
        "job_state": artifact.job_state,
        "reason_code": artifact.reason_code,
        "created_at": artifact.created_at.isoformat(),
        "expires_at": artifact.expires_at.isoformat(),
        "policy_id": artifact.policy_id,
        "trace_digest": artifact.trace_digest,
        "recommended_actions": list(artifact.recommended_actions),
        "evidence": [asdict(item) for item in artifact.evidence],
        "unresolved_question_codes": list(artifact.unresolved_question_codes),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def audit_handoff(
    artifact: HandoffArtifact,
    context: HandoffContext,
    policy: HandoffPolicy,
    *,
    now: datetime,
) -> HandoffDecision:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(UTC)
    findings: set[str] = set()
    if artifact.reason_code not in policy.allowed_reasons:
        findings.add("reason_not_allowed")
    if artifact.job_state not in policy.allowed_job_states:
        findings.add("job_state_not_handoff_eligible")
    if artifact.created_at > now + timedelta(seconds=policy.max_future_skew_seconds):
        findings.add("handoff_future_dated")
    if artifact.expires_at <= now:
        findings.add("handoff_expired")
    if (artifact.expires_at - artifact.created_at).total_seconds() > policy.max_lifetime_seconds:
        findings.add("handoff_lifetime_exceeded")
    if artifact.issue_id != context.issue_id:
        findings.add("issue_binding_mismatch")
    if artifact.issue_version != context.issue_version:
        findings.add("issue_version_drift")
    if artifact.job_id != context.job_id:
        findings.add("job_binding_mismatch")
    if artifact.capability != context.capability:
        findings.add("capability_drift")
    if artifact.job_state != context.job_state:
        findings.add("job_state_drift")
    if artifact.policy_id != context.policy_id:
        findings.add("policy_drift")
    if artifact.trace_digest != context.trace_digest:
        findings.add("trace_binding_mismatch")
    if not artifact.recommended_actions:
        findings.add("recommended_action_required")
    if len(artifact.recommended_actions) > policy.max_actions:
        findings.add("action_budget_exceeded")
    if any(action not in context.allowed_actions for action in artifact.recommended_actions):
        findings.add("action_not_allowed")
    if len(artifact.evidence) > policy.max_evidence:
        findings.add("evidence_budget_exceeded")
    if policy.require_evidence and not artifact.evidence:
        findings.add("evidence_required")
    for reference in artifact.evidence:
        trusted = context.trusted_evidence.get(reference.evidence_id)
        if trusted is None:
            findings.add("evidence_not_trusted")
        else:
            if trusted.sha256 != reference.sha256:
                findings.add("evidence_digest_mismatch")
            if trusted.producer_call_id != reference.producer_call_id:
                findings.add("evidence_producer_mismatch")
    if len(artifact.unresolved_question_codes) > policy.max_questions:
        findings.add("question_budget_exceeded")
    evidence_id = hashlib.sha256(_canonical_artifact(artifact)).hexdigest()
    ordered = tuple(sorted(findings))
    return HandoffDecision(
        accepted=not ordered,
        finding_codes=ordered,
        evidence_id=evidence_id,
        action_count=len(artifact.recommended_actions),
        evidence_count=len(artifact.evidence),
        unresolved_question_count=len(artifact.unresolved_question_codes),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate an agent-to-human handoff artifact")
    parser.add_argument("artifact", type=Path)
    args = parser.parse_args(argv)
    try:
        artifact = parse_handoff(args.artifact.read_bytes())
    except (OSError, HandoffFormatError) as exc:
        print(json.dumps({"accepted": False, "error": type(exc).__name__}, sort_keys=True))
        return 3
    print(
        json.dumps(
            {
                "parsed": True,
                "handoff_evidence_id": hashlib.sha256(_canonical_artifact(artifact)).hexdigest(),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
