from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from service_desk.handoff import (
    HandoffContext,
    HandoffFormatError,
    HandoffPolicy,
    TrustedEvidence,
    audit_handoff,
    main,
    parse_handoff,
)

NOW = datetime(2026, 9, 28, 9, 0, tzinfo=UTC)
DIGEST = "a" * 64
EVIDENCE_DIGEST = "b" * 64


def document() -> dict[str, object]:
    return {
        "schema_version": 1,
        "handoff_id": "handoff-1",
        "issue_id": "issue-7",
        "issue_version": 4,
        "job_id": "job-3",
        "capability": "recommend_action",
        "job_state": "waiting_for_approval",
        "reason_code": "high_impact_action",
        "created_at": "2026-09-28T08:59:00Z",
        "expires_at": "2026-09-28T09:09:00Z",
        "policy_id": "handoff-v1",
        "trace_digest": DIGEST,
        "recommended_actions": ["assign_issue"],
        "evidence": [
            {
                "evidence_id": "evidence-1",
                "sha256": EVIDENCE_DIGEST,
                "producer_call_id": "call-8",
            }
        ],
        "unresolved_question_codes": ["confirm_owner"],
    }


def artifact(**updates: object):
    payload = document()
    payload.update(updates)
    return parse_handoff(json.dumps(payload).encode())


def context(**updates: object) -> HandoffContext:
    values = {
        "issue_id": "issue-7",
        "issue_version": 4,
        "job_id": "job-3",
        "capability": "recommend_action",
        "job_state": "waiting_for_approval",
        "policy_id": "handoff-v1",
        "trace_digest": DIGEST,
        "allowed_actions": frozenset({"assign_issue", "request_information"}),
        "trusted_evidence": {"evidence-1": TrustedEvidence(EVIDENCE_DIGEST, "call-8")},
    }
    values.update(updates)
    return HandoffContext(**values)  # type: ignore[arg-type]


def policy(**updates: object) -> HandoffPolicy:
    values = {"allowed_reasons": frozenset({"high_impact_action", "low_confidence"})}
    values.update(updates)
    return HandoffPolicy(**values)  # type: ignore[arg-type]


def decide(item=None, ctx=None, rules=None, *, now=NOW):
    return audit_handoff(item or artifact(), ctx or context(), rules or policy(), now=now)


def test_accepts_exact_current_handoff() -> None:
    decision = decide()
    assert decision.accepted
    assert decision.finding_codes == ()
    assert (decision.action_count, decision.evidence_count, decision.unresolved_question_count) == (
        1,
        1,
        1,
    )


@pytest.mark.parametrize(
    ("artifact_updates", "context_updates", "code"),
    [
        ({"issue_id": "issue-8"}, {}, "issue_binding_mismatch"),
        ({"issue_version": 3}, {}, "issue_version_drift"),
        ({"job_id": "job-4"}, {}, "job_binding_mismatch"),
        ({"capability": "triage"}, {}, "capability_drift"),
        ({"job_state": "failed"}, {}, "job_state_drift"),
        ({"policy_id": "handoff-v0"}, {}, "policy_drift"),
        ({"trace_digest": "c" * 64}, {}, "trace_binding_mismatch"),
    ],
)
def test_rejects_context_drift(artifact_updates, context_updates, code) -> None:
    assert code in decide(artifact(**artifact_updates), context(**context_updates)).finding_codes


def test_rejects_expired_future_dated_and_overlong_handoffs() -> None:
    expired = decide(artifact(expires_at="2026-09-28T08:59:59Z"))
    future = decide(artifact(created_at="2026-09-28T09:00:31Z", expires_at="2026-09-28T09:01:31Z"))
    overlong = decide(
        artifact(created_at="2026-09-28T08:00:00Z", expires_at="2026-09-28T09:00:01Z")
    )
    assert expired.finding_codes == ("handoff_expired",)
    assert future.finding_codes == ("handoff_future_dated",)
    assert overlong.finding_codes == ("handoff_lifetime_exceeded",)


def test_rejects_unapproved_reason_state_and_action() -> None:
    decision = decide(
        artifact(
            reason_code="routine",
            job_state="succeeded",
            recommended_actions=["delete_issue"],
        ),
        context(job_state="succeeded"),
    )
    assert decision.finding_codes == (
        "action_not_allowed",
        "job_state_not_handoff_eligible",
        "reason_not_allowed",
    )


def test_requires_action_and_evidence() -> None:
    decision = decide(artifact(recommended_actions=[], evidence=[]))
    assert decision.finding_codes == ("evidence_required", "recommended_action_required")


def test_evidence_must_match_trusted_catalog() -> None:
    missing = decide(artifact(), context(trusted_evidence={}))
    changed = decide(
        artifact(),
        context(trusted_evidence={"evidence-1": TrustedEvidence("c" * 64, "call-9")}),
    )
    assert missing.finding_codes == ("evidence_not_trusted",)
    assert changed.finding_codes == (
        "evidence_digest_mismatch",
        "evidence_producer_mismatch",
    )


def test_all_report_budgets_are_enforced() -> None:
    many_actions = [f"action-{index}" for index in range(3)]
    many_questions = [f"question-{index}" for index in range(3)]
    item = artifact(recommended_actions=many_actions, unresolved_question_codes=many_questions)
    decision = decide(
        item,
        context(allowed_actions=frozenset(many_actions)),
        policy(max_actions=2, max_questions=2),
    )
    assert decision.finding_codes == ("action_budget_exceeded", "question_budget_exceeded")


def test_evidence_budget_is_enforced() -> None:
    evidence = [
        {"evidence_id": f"e-{index}", "sha256": EVIDENCE_DIGEST, "producer_call_id": "call-8"}
        for index in range(3)
    ]
    trusted = {item["evidence_id"]: TrustedEvidence(EVIDENCE_DIGEST, "call-8") for item in evidence}
    decision = decide(
        artifact(evidence=evidence), context(trusted_evidence=trusted), policy(max_evidence=2)
    )
    assert decision.finding_codes == ("evidence_budget_exceeded",)


def test_evidence_id_is_deterministic_and_content_addressed() -> None:
    first = decide()
    reordered = json.dumps(document(), sort_keys=True).encode()
    second = decide(parse_handoff(reordered))
    changed = decide(artifact(unresolved_question_codes=["confirm_scope"]))
    assert first.evidence_id == second.evidence_id
    assert first.evidence_id != changed.evidence_id
    assert len(first.evidence_id) == 64


@pytest.mark.parametrize(
    "mutate",
    [
        lambda item: item.update(extra="field"),
        lambda item: item.update(schema_version=2),
        lambda item: item.update(issue_version=True),
        lambda item: item.update(created_at="2026-09-28T09:00:00"),
        lambda item: item.update(expires_at="2026-09-28T08:00:00Z"),
        lambda item: item.update(trace_digest="A" * 64),
        lambda item: item.update(recommended_actions=["same", "same"]),
        lambda item: item.update(evidence=[item["evidence"][0], item["evidence"][0]]),
    ],
)
def test_malformed_handoffs_fail_closed(mutate) -> None:
    payload = document()
    mutate(payload)
    with pytest.raises(HandoffFormatError):
        parse_handoff(json.dumps(payload).encode())


def test_duplicate_json_and_non_finite_values_fail_closed() -> None:
    with pytest.raises(HandoffFormatError, match="duplicate"):
        parse_handoff(b'{"schema_version":1,"schema_version":1}')
    raw = json.dumps(document()).replace('"issue_version": 4', '"issue_version": NaN')
    with pytest.raises(HandoffFormatError, match="non-finite"):
        parse_handoff(raw.encode())


def test_size_limit_is_checked_before_parsing() -> None:
    with pytest.raises(HandoffFormatError, match="size"):
        parse_handoff(b" " * (128 * 1024 + 1))


def test_now_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        decide(now=datetime.fromisoformat("2026-09-28T09:00:00"))


def test_policy_configuration_is_bounded() -> None:
    with pytest.raises(ValueError):
        policy(max_lifetime_seconds=0)
    with pytest.raises(ValueError):
        policy(max_future_skew_seconds=301)
    with pytest.raises(ValueError):
        policy(max_evidence=129)


def test_decision_does_not_disclose_identifiers() -> None:
    rendered = json.dumps(decide().to_dict(), sort_keys=True)
    assert "issue-7" not in rendered
    assert "job-3" not in rendered
    assert "evidence-1" not in rendered


def test_cli_parses_valid_artifact_without_echoing_contents(tmp_path, capsys) -> None:
    path = tmp_path / "handoff.json"
    path.write_text(json.dumps(document()))
    assert main([str(path)]) == 0
    output = capsys.readouterr().out
    assert '"parsed": true' in output
    assert "issue-7" not in output


def test_cli_uses_separate_malformed_exit_code(tmp_path, capsys) -> None:
    path = tmp_path / "handoff.json"
    path.write_text("not-json")
    assert main([str(path)]) == 3
    assert "HandoffFormatError" in capsys.readouterr().out
