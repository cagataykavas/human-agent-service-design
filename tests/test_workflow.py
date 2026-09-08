from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from human_agent.audit import AuditLedger
from human_agent.domain import (
    AgentRecommendation,
    Evidence,
    Impact,
    Recommendation,
    ServiceCase,
)
from human_agent.review_queue import ReviewQueue
from human_agent.workflow import CaseState, CaseWorkflow, InvalidTransition, VersionConflict


class ManualClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 8, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


def make_case(case_id: str, *, impact: Impact = Impact.HIGH) -> ServiceCase:
    return ServiceCase(
        case_id=case_id,
        customer_segment="retail",
        impact=impact,
        evidence=[Evidence("id-1", "identity", "registry", 0.98)],
        agent=AgentRecommendation(
            Recommendation.APPROVE,
            confidence=0.96,
            reason_codes=("identity_match",),
            explanation="Identity verified.",
            evidence_ids=("id-1",),
        ),
    )


def test_human_review_lifecycle_is_versioned_and_audited() -> None:
    clock = ManualClock()
    ledger = AuditLedger(clock)
    queue = ReviewQueue(clock)
    workflow = CaseWorkflow(ledger=ledger, review_queue=queue)
    record = workflow.submit(make_case("case-1"))

    decision = workflow.route("case-1", expected_version=record.version)
    assert decision.reviewer_priority == 70
    assert record.state is CaseState.QUEUED_FOR_REVIEW
    assert len(queue) == 1

    accepted = workflow.accept_review("reviewer-7", expected_version=record.version)
    assert accepted is record
    assert record.state is CaseState.UNDER_REVIEW

    completed = workflow.decide(
        "case-1",
        "reviewer-7",
        Recommendation.REJECT,
        expected_version=record.version,
        rationale="Document evidence conflicts with registry policy.",
    )
    assert completed.state is CaseState.DECIDED
    assert completed.decision is Recommendation.REJECT
    assert len(queue) == 0
    assert [event.event_type for event in ledger.stream("case-1")] == [
        "case_received",
        "case_routed",
        "review_started",
        "review_decided",
    ]
    assert ledger.verify("case-1") is True
    assert ledger.stream("case-1")[-1].payload["overrode_agent"] is True


def test_stale_version_and_wrong_reviewer_are_rejected() -> None:
    workflow = CaseWorkflow()
    record = workflow.submit(make_case("case-2"))
    workflow.route("case-2", expected_version=1)
    with pytest.raises(VersionConflict):
        workflow.route("case-2", expected_version=1)

    workflow.accept_review("reviewer-a", expected_version=record.version)
    with pytest.raises(PermissionError):
        workflow.decide(
            "case-2",
            "reviewer-b",
            Recommendation.APPROVE,
            expected_version=record.version,
            rationale="Looks valid.",
        )


def test_automated_case_cannot_be_manually_decided() -> None:
    workflow = CaseWorkflow()
    record = workflow.submit(make_case("case-3", impact=Impact.LOW))
    workflow.route("case-3", expected_version=record.version)
    assert record.state is CaseState.DECIDED
    assert record.decision is Recommendation.APPROVE
    with pytest.raises(InvalidTransition):
        workflow.decide(
            "case-3",
            "reviewer-a",
            Recommendation.REJECT,
            expected_version=record.version,
            rationale="Manual override attempt.",
        )


def test_review_queue_orders_priority_and_releases_expired_leases() -> None:
    clock = ManualClock()
    queue = ReviewQueue(clock)
    queue.enqueue("medium", 50, ("low_confidence",))
    queue.enqueue("urgent", 95, ("mandatory_review",))

    urgent = queue.lease("reviewer-a", duration=timedelta(minutes=5))
    assert urgent is not None and urgent.case_id == "urgent"
    medium = queue.lease("reviewer-b")
    assert medium is not None and medium.case_id == "medium"

    clock.advance(timedelta(minutes=6))
    released = queue.lease("reviewer-c")
    assert released is urgent
    assert released.leased_by == "reviewer-c"


def test_domain_rejects_recommendation_with_unknown_evidence() -> None:
    with pytest.raises(ValueError, match="unknown evidence"):
        ServiceCase(
            case_id="broken",
            customer_segment="retail",
            impact=Impact.LOW,
            evidence=[],
            agent=AgentRecommendation(
                Recommendation.APPROVE,
                confidence=0.9,
                reason_codes=(),
                explanation="Approved.",
                evidence_ids=("missing",),
            ),
        )
