from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from service_desk.errors import Conflict, Forbidden
from service_desk.models import Actor, ActorRole, IssueLinkType, IssuePriority
from service_desk.repository import SQLiteServiceDeskRepository
from service_desk.service import ServiceDesk

ADMIN = Actor("admin-1", ActorRole.ADMIN)
AGENT = Actor("agent-1", ActorRole.AGENT)
REQUESTER = Actor("customer-1", ActorRole.REQUESTER)


class ManualClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 11, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


def build_desk(tmp_path: Path) -> tuple[ServiceDesk, ManualClock]:
    clock = ManualClock()
    desk = ServiceDesk(SQLiteServiceDeskRepository(tmp_path / "desk.db", clock=clock))
    desk.bootstrap()
    desk.create_project(key="OPS", name="Operations", project_id="project-1")
    return desk, clock


def create_issue(desk: ServiceDesk, sequence: int, *, project_id: str = "project-1"):
    return desk.create_issue(
        project_id=project_id,
        summary=f"Service request {sequence}",
        description="A reproducible customer-impacting incident.",
        priority=IssuePriority.HIGH,
        reporter=REQUESTER,
        idempotency_key=f"request-{project_id}-{sequence}",
        issue_id=f"issue-{project_id}-{sequence}",
    )


def test_sla_is_instantiated_and_first_human_response_is_recorded(tmp_path: Path) -> None:
    desk, clock = build_desk(tmp_path)
    policy = desk.configure_sla(
        project_id="project-1",
        priority=IssuePriority.HIGH,
        first_response_seconds=900,
        resolution_seconds=3600,
        actor=ADMIN,
    )
    assert policy.resolution_seconds == 3600
    with pytest.raises(Forbidden):
        desk.configure_sla(
            project_id="project-1",
            priority=IssuePriority.LOW,
            first_response_seconds=900,
            resolution_seconds=3600,
            actor=AGENT,
        )

    issue = create_issue(desk, 1)
    initial = desk.sla_state(issue.issue_id)
    assert initial.first_response_remaining_seconds == 900
    assert initial.resolution_remaining_seconds == 3600

    clock.advance(seconds=300)
    desk.comment(issue.issue_id, "Any news?", actor=REQUESTER)
    assert desk.sla_state(issue.issue_id).first_responded_at is None
    clock.advance(seconds=120)
    desk.comment(issue.issue_id, "We are investigating.", actor=AGENT)
    responded = desk.sla_state(issue.issue_id)
    assert responded.first_responded_at == clock.value
    assert responded.first_response_breached is False
    assert responded.first_response_remaining_seconds == 480


def test_sla_clock_pauses_for_customer_and_preserves_remaining_budget(tmp_path: Path) -> None:
    desk, clock = build_desk(tmp_path)
    desk.configure_sla(
        project_id="project-1",
        priority=IssuePriority.HIGH,
        first_response_seconds=600,
        resolution_seconds=1800,
        actor=ADMIN,
    )
    issue = create_issue(desk, 1)
    triaged = desk.transition(issue.issue_id, "triage", actor=AGENT, expected_version=issue.version)
    started = desk.transition(
        issue.issue_id, "start", actor=AGENT, expected_version=triaged.version
    )
    clock.advance(seconds=300)
    waiting = desk.transition(
        issue.issue_id,
        "request_information",
        actor=AGENT,
        expected_version=started.version,
    )
    paused = desk.sla_state(issue.issue_id)
    assert paused.resolution_remaining_seconds == 1500

    clock.advance(hours=8)
    still_paused = desk.sla_state(issue.issue_id)
    assert still_paused.resolution_remaining_seconds == 1500
    resumed = desk.transition(
        issue.issue_id,
        "customer_replied",
        actor=REQUESTER,
        expected_version=waiting.version,
    )
    state = desk.sla_state(issue.issue_id)
    assert resumed.status.value == "in_progress"
    assert state.paused_at is None
    assert state.total_paused_seconds == 8 * 3600
    assert state.resolution_remaining_seconds == 1500

    clock.advance(seconds=1501)
    assert desk.sla_state(issue.issue_id).resolution_breached is True


def test_resolution_freezes_observed_sla_result(tmp_path: Path) -> None:
    desk, clock = build_desk(tmp_path)
    desk.configure_sla(
        project_id="project-1",
        priority=IssuePriority.HIGH,
        first_response_seconds=60,
        resolution_seconds=120,
        actor=ADMIN,
    )
    issue = create_issue(desk, 1)
    triaged = desk.transition(issue.issue_id, "triage", actor=AGENT, expected_version=1)
    started = desk.transition(
        issue.issue_id, "start", actor=AGENT, expected_version=triaged.version
    )
    clock.advance(seconds=121)
    desk.transition(
        issue.issue_id,
        "resolve",
        actor=AGENT,
        expected_version=started.version,
        resolution="Recovered and verified.",
    )
    completed = desk.sla_state(issue.issue_id)
    assert completed.resolution_breached is True
    clock.advance(days=30)
    assert desk.sla_state(issue.issue_id) == completed


def test_hierarchy_rejects_cycles_second_parents_and_cross_project_children(
    tmp_path: Path,
) -> None:
    desk, _ = build_desk(tmp_path)
    root, child, grandchild = (create_issue(desk, number) for number in range(1, 4))
    desk.link_issues(
        source_issue_id=root.issue_id,
        target_issue_id=child.issue_id,
        link_type=IssueLinkType.PARENT_OF,
        actor=AGENT,
        link_id="root-child",
    )
    desk.link_issues(
        source_issue_id=child.issue_id,
        target_issue_id=grandchild.issue_id,
        link_type=IssueLinkType.PARENT_OF,
        actor=ADMIN,
        link_id="child-grandchild",
    )
    assert [item.issue_id for item in desk.repository.issue_descendants(root.issue_id)] == [
        child.issue_id,
        grandchild.issue_id,
    ]
    with pytest.raises(Conflict, match="cycle"):
        desk.link_issues(
            source_issue_id=grandchild.issue_id,
            target_issue_id=root.issue_id,
            link_type=IssueLinkType.PARENT_OF,
            actor=AGENT,
        )
    with pytest.raises(Conflict, match="already has a parent"):
        desk.link_issues(
            source_issue_id=root.issue_id,
            target_issue_id=grandchild.issue_id,
            link_type=IssueLinkType.PARENT_OF,
            actor=AGENT,
        )

    desk.create_project(key="SEC", name="Security", project_id="project-2")
    external = create_issue(desk, 1, project_id="project-2")
    with pytest.raises(Conflict, match="same project"):
        desk.link_issues(
            source_issue_id=root.issue_id,
            target_issue_id=external.issue_id,
            link_type=IssueLinkType.PARENT_OF,
            actor=AGENT,
        )
    with pytest.raises(Forbidden):
        desk.link_issues(
            source_issue_id=root.issue_id,
            target_issue_id=external.issue_id,
            link_type=IssueLinkType.RELATES_TO,
            actor=REQUESTER,
        )


def test_link_creation_emits_audit_and_outbox_evidence(tmp_path: Path) -> None:
    desk, clock = build_desk(tmp_path)
    parent, child = create_issue(desk, 1), create_issue(desk, 2)
    link = desk.link_issues(
        source_issue_id=parent.issue_id,
        target_issue_id=child.issue_id,
        link_type=IssueLinkType.BLOCKS,
        actor=AGENT,
        link_id="blocking-link",
    )
    assert link.created_at == clock.value
    assert "issue_link_created" in {
        event["event_type"] for event in desk.repository.audit_stream(child.issue_id)
    }
    event_types = [event.event_type for event in desk.repository.pending_outbox("publisher", 10)]
    assert event_types.count("issue.link_created") == 1
