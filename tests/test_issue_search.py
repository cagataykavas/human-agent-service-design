from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from service_desk.errors import InvalidIssueQuery
from service_desk.models import Actor, ActorRole, IssuePriority
from service_desk.repository import SQLiteServiceDeskRepository
from service_desk.search import IssueCursor, compile_issue_query
from service_desk.service import ServiceDesk


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 15, 8, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


def setup(tmp_path: Path) -> tuple[ServiceDesk, Clock, str]:
    clock = Clock()
    desk = ServiceDesk(SQLiteServiceDeskRepository(tmp_path / "search.db", clock=clock))
    desk.bootstrap()
    project = desk.create_project(key="OPS", name="Operations")
    desk.configure_sla(
        project_id=project.project_id,
        priority=IssuePriority.HIGH,
        first_response_seconds=300,
        resolution_seconds=1200,
        actor=Actor("admin", ActorRole.ADMIN),
    )
    for number, (summary, priority, labels) in enumerate(
        (
            ("VPN authentication fails", IssuePriority.HIGH, ("vpn", "production")),
            ("Laptop replacement", IssuePriority.LOW, ("hardware",)),
            ("VPN client installation", IssuePriority.HIGH, ("vpn",)),
        ),
        start=1,
    ):
        desk.create_issue(
            project_id=project.project_id,
            summary=summary,
            description=f"Reproducible request {number}",
            priority=priority,
            reporter=Actor("customer", ActorRole.REQUESTER),
            idempotency_key=f"request-{number}",
            issue_id=f"issue-{number}",
            labels=labels,
        )
    return desk, clock, project.project_id


def test_compound_query_uses_allowlisted_filters_and_bound_values(tmp_path: Path) -> None:
    desk, _, project_id = setup(tmp_path)
    page = desk.repository.search_issues(
        project_id, 'priority IN (high,critical) AND label = vpn AND text ~ "auth"'
    )
    assert [issue.issue_key for issue in page.items] == ["OPS-1"]
    compiled = compile_issue_query("assignee != nobody")
    assert "nobody" not in compiled.where_sql
    assert compiled.parameters == ("nobody",)


@pytest.mark.parametrize(
    "query",
    (
        "status = open; DROP TABLE issues",
        "project = OPS",
        "text = vpn",
        "priority IN ()",
        "status = imaginary",
        "status = open OR priority = high",
    ),
)
def test_unsupported_or_injection_shaped_queries_are_rejected(query: str) -> None:
    with pytest.raises(InvalidIssueQuery):
        compile_issue_query(query)


def test_keyset_cursor_has_no_duplicates_when_timestamps_match(tmp_path: Path) -> None:
    desk, _, project_id = setup(tmp_path)
    first = desk.repository.search_issues(project_id, None, limit=2)
    second = desk.repository.search_issues(project_id, None, limit=2, cursor=first.next_cursor)
    assert [issue.issue_key for issue in first.items] == ["OPS-3", "OPS-2"]
    assert [issue.issue_key for issue in second.items] == ["OPS-1"]
    assert set(first.items).isdisjoint(second.items)
    assert second.next_cursor is None
    with pytest.raises(InvalidIssueQuery, match="cursor"):
        IssueCursor.decode("not-valid")


def test_sla_operational_filters_use_repository_clock(tmp_path: Path) -> None:
    desk, clock, project_id = setup(tmp_path)
    assert {
        issue.issue_key
        for issue in desk.repository.search_issues(project_id, "sla = at_risk").items
    } == {
        "OPS-1",
        "OPS-3",
    }
    clock.value += timedelta(seconds=1201)
    assert {
        issue.issue_key
        for issue in desk.repository.search_issues(project_id, "sla = breached").items
    } == {
        "OPS-1",
        "OPS-3",
    }
    assert [
        issue.issue_key for issue in desk.repository.search_issues(project_id, "sla = none").items
    ] == ["OPS-2"]
