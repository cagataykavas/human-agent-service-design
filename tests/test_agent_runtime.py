from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from service_desk.agent import AgentWorker, DeterministicAgentModel
from service_desk.errors import Conflict, ToolPolicyViolation
from service_desk.models import Actor, ActorRole, AgentJobState, IssuePriority
from service_desk.repository import SQLiteServiceDeskRepository
from service_desk.service import ServiceDesk
from service_desk.tools import KnowledgeSearchTool, ReadOnlySQLTool, ToolContext, ToolRegistry


def setup_desk(tmp_path: Path):
    repository = SQLiteServiceDeskRepository(tmp_path / "desk.db")
    desk = ServiceDesk(repository)
    desk.bootstrap()
    desk.create_project(key="SEC", name="Security", project_id="project-1")
    issue = desk.create_issue(
        project_id="project-1",
        summary="Production VPN outage",
        description="All developers are unable to authenticate.",
        priority=IssuePriority.HIGH,
        reporter=Actor("user-1", ActorRole.REQUESTER),
        idempotency_key="request-1",
        issue_id="issue-1",
    )
    return repository, desk, issue


def test_agent_job_claim_is_leased_and_only_owner_can_finish(tmp_path: Path) -> None:
    repository, desk, issue = setup_desk(tmp_path)
    desk.request_agent_job(
        issue.issue_id,
        "triage",
        {"summary": issue.summary, "description": issue.description},
        actor=Actor("agent-1", ActorRole.AGENT),
        job_id="job-1",
    )
    claimed = repository.claim_agent_job("worker-a")
    assert claimed is not None
    assert claimed.state is AgentJobState.RUNNING
    assert claimed.attempt == 1
    assert repository.claim_agent_job("worker-b") is None
    with pytest.raises(Conflict, match="active worker"):
        repository.finish_agent_job(
            claimed.job_id,
            "worker-b",
            AgentJobState.SUCCEEDED,
            output={"category": "incident"},
        )
    finished = repository.finish_agent_job(
        claimed.job_id,
        "worker-a",
        AgentJobState.SUCCEEDED,
        output={"category": "incident"},
    )
    assert finished.output_payload == {"category": "incident"}


def test_expired_job_lease_is_recoverable(tmp_path: Path) -> None:
    class Clock:
        value = datetime(2026, 9, 11, tzinfo=UTC)

        def __call__(self):
            return self.value

    clock = Clock()
    repository = SQLiteServiceDeskRepository(tmp_path / "desk.db", clock=clock)
    desk = ServiceDesk(repository)
    desk.bootstrap()
    desk.create_project(key="OPS", name="Ops", project_id="project-1")
    issue = desk.create_issue(
        project_id="project-1",
        summary="Request",
        description="",
        priority=IssuePriority.LOW,
        reporter=Actor("user", ActorRole.REQUESTER),
        idempotency_key="one",
    )
    desk.request_agent_job(
        issue.issue_id,
        "triage",
        {"summary": issue.summary},
        actor=Actor("agent", ActorRole.AGENT),
        job_id="job-1",
    )
    repository.claim_agent_job("worker-a", lease_seconds=30)
    clock.value += timedelta(seconds=31)
    reclaimed = repository.claim_agent_job("worker-b")
    assert reclaimed is not None
    assert reclaimed.attempt == 2
    assert reclaimed.lease_owner == "worker-b"


def test_worker_executes_retrieval_through_allowlisted_tool(tmp_path: Path) -> None:
    repository, desk, issue = setup_desk(tmp_path)
    desk.request_agent_job(
        issue.issue_id,
        "retrieve_knowledge",
        {"query": "VPN authentication runbook"},
        actor=Actor("agent-1", ActorRole.AGENT),
        job_id="job-1",
    )
    tools = ToolRegistry()
    tools.register(
        KnowledgeSearchTool(
            {
                "kb-1": "VPN authentication credential reset runbook",
                "kb-2": "Laptop replacement policy",
            }
        )
    )
    result = AgentWorker(
        repository,
        desk,
        DeterministicAgentModel(),
        tools,
        worker_id="worker-a",
    ).run_once()
    assert result is not None
    assert result.job.state is AgentJobState.SUCCEEDED
    assert result.job.output_payload["evidence"][0]["document_id"] == "kb-1"
    assert len(result.tool_calls) == 1


def test_sql_tool_is_read_only_allowlisted_and_bounded(tmp_path: Path) -> None:
    repository, _, _ = setup_desk(tmp_path)
    tool = ReadOnlySQLTool(repository.path, max_rows=1)
    context = ToolContext("worker-a", "job-1")
    result = tool.execute(
        {
            "sql": "SELECT issue_key, status FROM issues WHERE project_id = :project_id",
            "parameters": {"project_id": "project-1"},
        },
        context,
    )
    assert result["row_count"] == 1
    assert result["rows"][0]["issue_key"] == "SEC-1"
    with pytest.raises(ToolPolicyViolation, match="forbidden"):
        tool.execute({"sql": "DELETE FROM issues"}, context)
    with pytest.raises(ToolPolicyViolation, match="non-allowlisted"):
        tool.execute({"sql": "SELECT * FROM audit_events"}, context)
    with pytest.raises(ToolPolicyViolation, match="multiple"):
        tool.execute({"sql": "SELECT * FROM issues; SELECT * FROM projects"}, context)


def test_write_tool_requires_independent_approval(tmp_path: Path) -> None:
    repository, desk, issue = setup_desk(tmp_path)
    job = desk.request_agent_job(
        issue.issue_id,
        "recommend_action",
        {},
        actor=Actor("agent-1", ActorRole.AGENT),
        job_id="job-1",
    )
    claimed = repository.claim_agent_job("worker-a")
    assert claimed is not None
    call = desk.request_tool_call(
        job_id=job.job_id,
        tool_name="transition_issue",
        arguments={"issue_id": issue.issue_id},
        requested_by="worker-a",
        call_id="call-1",
    )
    assert call.state == "waiting_for_approval"
    with pytest.raises(Conflict, match="different approver"):
        repository.approve_tool_call(call.call_id, "worker-a")
    approved = desk.approve_tool_call(
        call.call_id,
        actor=Actor("human-reviewer", ActorRole.AGENT),
    )
    assert approved.state == "approved"
    assert approved.approved_by == "human-reviewer"
