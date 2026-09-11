from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from service_desk.errors import Conflict, Forbidden, InvalidTransition
from service_desk.models import Actor, ActorRole, IssuePriority, IssueStatus
from service_desk.repository import SQLiteServiceDeskRepository
from service_desk.service import ServiceDesk


class ManualClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 11, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


@pytest.fixture
def desk(tmp_path: Path) -> ServiceDesk:
    repository = SQLiteServiceDeskRepository(tmp_path / "desk.db")
    service = ServiceDesk(repository)
    service.bootstrap()
    service.create_project(key="OPS", name="Operations", project_id="project-1")
    return service


def create_issue(desk: ServiceDesk, *, key: str = "request-1"):
    return desk.create_issue(
        project_id="project-1",
        summary="VPN access fails",
        description="Client reports expired credentials.",
        priority=IssuePriority.HIGH,
        reporter=Actor("user-1", ActorRole.REQUESTER),
        idempotency_key=key,
        labels=("Access", "vpn", "access"),
        issue_id="issue-1",
    )


def test_issue_creation_is_idempotent_and_allocates_human_key(desk: ServiceDesk) -> None:
    first = create_issue(desk)
    replay = create_issue(desk)
    assert first.issue_id == replay.issue_id
    assert first.issue_key == "OPS-1"
    assert first.labels == ("access", "vpn")
    with pytest.raises(Conflict, match="different request"):
        desk.create_issue(
            project_id="project-1",
            summary="Different request",
            description="",
            priority=IssuePriority.LOW,
            reporter=Actor("user-1", ActorRole.REQUESTER),
            idempotency_key="request-1",
        )


def test_workflow_enforces_roles_resolution_and_optimistic_version(desk: ServiceDesk) -> None:
    issue = create_issue(desk)
    with pytest.raises(Forbidden):
        desk.transition(
            issue.issue_id,
            "triage",
            actor=Actor("user-1", ActorRole.REQUESTER),
            expected_version=issue.version,
        )

    triaged = desk.transition(
        issue.issue_id,
        "triage",
        actor=Actor("triage-agent", ActorRole.AI_WORKER),
        expected_version=issue.version,
    )
    assert triaged.status is IssueStatus.TRIAGED
    with pytest.raises(Conflict, match="expected issue version"):
        desk.transition(
            issue.issue_id,
            "start",
            actor=Actor("agent-1", ActorRole.AGENT),
            expected_version=issue.version,
        )

    started = desk.transition(
        issue.issue_id,
        "start",
        actor=Actor("agent-1", ActorRole.AGENT),
        expected_version=triaged.version,
    )
    with pytest.raises(InvalidTransition, match="resolution text"):
        desk.transition(
            issue.issue_id,
            "resolve",
            actor=Actor("agent-1", ActorRole.AGENT),
            expected_version=started.version,
        )
    resolved = desk.transition(
        issue.issue_id,
        "resolve",
        actor=Actor("agent-1", ActorRole.AGENT),
        expected_version=started.version,
        resolution="Credential reset completed and verified.",
    )
    assert resolved.status is IssueStatus.RESOLVED
    assert resolved.resolution.startswith("Credential reset")


def test_assignment_requires_agent_role_and_increments_version(desk: ServiceDesk) -> None:
    issue = create_issue(desk)
    with pytest.raises(Forbidden):
        desk.assign(
            issue.issue_id,
            "agent-2",
            actor=Actor("user-1", ActorRole.REQUESTER),
            expected_version=issue.version,
        )
    assigned = desk.assign(
        issue.issue_id,
        "agent-2",
        actor=Actor("lead-1", ActorRole.ADMIN),
        expected_version=issue.version,
    )
    assert assigned.assignee_id == "agent-2"
    assert assigned.version == issue.version + 1


def test_issue_and_outbox_event_commit_together(tmp_path: Path) -> None:
    clock = ManualClock()
    repository = SQLiteServiceDeskRepository(tmp_path / "desk.db", clock=clock)
    desk = ServiceDesk(repository)
    desk.bootstrap()
    desk.create_project(key="OPS", name="Operations", project_id="project-1")
    create_issue(desk)

    leased = repository.pending_outbox("publisher-a", 10)
    assert [event.event_type for event in leased] == ["issue.created"]
    repository.reject_outbox(leased[0].event_id, "publisher-a", "broker down", retry_delay=60)
    assert repository.pending_outbox("publisher-b", 10) == []
    clock.advance(timedelta(seconds=61))
    retried = repository.pending_outbox("publisher-b", 10)
    assert retried[0].attempts == 1
    repository.acknowledge_outbox(retried[0].event_id, "publisher-b")
    assert repository.pending_outbox("publisher-c", 10) == []


def test_audit_stream_records_actor_and_transition(desk: ServiceDesk) -> None:
    issue = create_issue(desk)
    desk.transition(
        issue.issue_id,
        "triage",
        actor=Actor("ai-triage-1", ActorRole.AI_WORKER),
        expected_version=issue.version,
    )
    stream = desk.repository.audit_stream(issue.issue_id)
    assert [event["event_type"] for event in stream] == ["issue_created", "issue_transitioned"]
    assert stream[-1]["actor_id"] == "ai-triage-1"
    assert stream[-1]["payload"]["to"] == "triaged"


def test_internal_comments_are_hidden_from_requester_view(desk: ServiceDesk) -> None:
    issue = create_issue(desk)
    desk.comment(
        issue.issue_id,
        "Customer-visible update.",
        actor=Actor("agent-1", ActorRole.AGENT),
    )
    desk.comment(
        issue.issue_id,
        "Potential credential compromise; consult security.",
        actor=Actor("agent-1", ActorRole.AGENT),
        internal=True,
    )
    public = desk.repository.list_comments(issue.issue_id, include_internal=False)
    reviewer = desk.repository.list_comments(issue.issue_id, include_internal=True)
    assert [comment.body for comment in public] == ["Customer-visible update."]
    assert len(reviewer) == 2
    with pytest.raises(Forbidden, match="internal"):
        desk.comment(
            issue.issue_id,
            "I should not see this channel.",
            actor=Actor("user-1", ActorRole.REQUESTER),
            internal=True,
        )
