from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from service_desk.api import create_service_desk_router
from service_desk.models import Actor, ActorRole, IssuePriority
from service_desk.observability import collect_snapshot, database_ready, render_prometheus
from service_desk.repository import SQLiteServiceDeskRepository
from service_desk.service import ServiceDesk


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 15, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


def setup(tmp_path: Path) -> tuple[ServiceDesk, Clock]:
    clock = Clock()
    desk = ServiceDesk(SQLiteServiceDeskRepository(tmp_path / "operations.db", clock=clock))
    desk.bootstrap()
    project = desk.create_project(key="OPS", name="Operations", project_id="project-1")
    desk.configure_sla(
        project_id=project.project_id,
        priority=IssuePriority.HIGH,
        first_response_seconds=300,
        resolution_seconds=1200,
        actor=Actor("admin", ActorRole.ADMIN),
    )
    issue = desk.create_issue(
        project_id=project.project_id,
        summary="Identity provider unavailable",
        description="Customers cannot authenticate.",
        priority=IssuePriority.HIGH,
        reporter=Actor("customer", ActorRole.REQUESTER),
        idempotency_key="request-1",
        issue_id="issue-1",
    )
    desk.request_agent_job(
        issue.issue_id,
        "triage",
        {"summary": issue.summary},
        actor=Actor("agent", ActorRole.AGENT),
        job_id="job-1",
    )
    return desk, clock


def test_snapshot_exposes_backlog_jobs_and_sla_risk_without_high_cardinality(
    tmp_path: Path,
) -> None:
    desk, clock = setup(tmp_path)
    initial = collect_snapshot(desk.repository)
    assert initial.issues_by_status == {"open": 1}
    assert initial.agent_jobs_by_state == {"queued": 1}
    assert initial.outbox_pending == 2
    assert initial.sla_at_risk == 1
    assert initial.sla_resolution_breached == 0

    clock.value += timedelta(seconds=1201)
    breached = collect_snapshot(desk.repository)
    assert breached.sla_first_response_breached == 1
    assert breached.sla_resolution_breached == 1
    assert breached.sla_at_risk == 0
    assert breached.outbox_oldest_age_seconds == 1201
    output = render_prometheus(breached)
    assert 'service_desk_issues{status="open"} 1' in output
    assert 'service_desk_agent_jobs{state="queued"} 1' in output
    assert 'service_desk_sla_breached{objective="resolution"} 1' in output
    assert "issue-1" not in output


def test_operations_endpoints_are_scrapeable_and_ready(tmp_path: Path) -> None:
    desk, _ = setup(tmp_path)
    app = FastAPI()
    app.include_router(create_service_desk_router(desk))
    client = TestClient(app)
    readiness = client.get("/v1/operations/readiness")
    assert readiness.status_code == 200
    assert readiness.json() == {"status": "ready"}
    assert database_ready(desk.repository) is True
    metrics = client.get("/v1/operations/metrics")
    assert metrics.status_code == 200
    assert metrics.headers["content-type"].startswith("text/plain")
    assert "# TYPE service_desk_outbox_pending gauge" in metrics.text


def test_readiness_returns_false_for_unavailable_database(tmp_path: Path) -> None:
    repository = SQLiteServiceDeskRepository(tmp_path / "temporary.db")
    repository.path = str(tmp_path / "missing" / "unavailable.db")
    assert database_ready(repository) is False
