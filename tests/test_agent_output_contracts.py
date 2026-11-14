from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from service_desk.agent import AgentWorker
from service_desk.agent_contracts import failure_code, validate_model_output
from service_desk.errors import AgentOutputViolation, ToolPolicyViolation
from service_desk.models import Actor, ActorRole, AgentJobState, IssuePriority
from service_desk.repository import SQLiteServiceDeskRepository
from service_desk.service import ServiceDesk
from service_desk.tools import KnowledgeSearchTool, ToolRegistry


def setup_job(tmp_path: Path, capability: str, payload: dict[str, Any] | None = None):
    repository = SQLiteServiceDeskRepository(tmp_path / "desk.db")
    desk = ServiceDesk(repository)
    desk.bootstrap()
    project = desk.create_project(key="OPS", name="Operations", project_id="project-1")
    issue = desk.create_issue(
        project_id=project.project_id,
        summary="Production access request",
        description="Review the access runbook.",
        priority=IssuePriority.HIGH,
        reporter=Actor("requester", ActorRole.REQUESTER),
        idempotency_key="request-1",
        issue_id="issue-1",
    )
    desk.request_agent_job(
        issue.issue_id,
        capability,
        payload or {},
        actor=Actor("agent", ActorRole.AGENT),
        job_id="job-1",
    )
    return repository, desk


@pytest.mark.parametrize(
    ("capability", "output"),
    [
        (
            "triage",
            {
                "category": "security_incident",
                "priority": "critical",
                "confidence": 0.91,
                "reason_codes": ["credential_exposure"],
            },
        ),
        ("summarize", {"summary": "Account recovery is blocked.", "confidence": 1}),
        (
            "recommend_action",
            {
                "recommendation": "request_human_review",
                "confidence": 0.7,
                "requires_human": True,
            },
        ),
        ("retrieve_knowledge", {"query": "account recovery runbook", "confidence": 0.8}),
    ],
)
def test_valid_capability_outputs_are_normalized(capability: str, output: dict[str, Any]) -> None:
    normalized = validate_model_output(capability, output)

    assert normalized == output
    assert normalized is not output
    assert isinstance(normalized["confidence"], float)


@pytest.mark.parametrize(
    ("capability", "output", "message"),
    [
        ("summarize", {"summary": "ok", "confidence": float("nan")}, "finite"),
        ("summarize", {"summary": "ok", "confidence": True}, "numeric"),
        (
            "summarize",
            {"summary": "ok", "confidence": 0.8, "private_reasoning": "secret"},
            "unknown",
        ),
        (
            "triage",
            {
                "category": "incident",
                "priority": "urgent",
                "confidence": 0.8,
                "reason_codes": ["outage"],
            },
            "priority",
        ),
        (
            "triage",
            {
                "category": "incident",
                "priority": "high",
                "confidence": 0.8,
                "reason_codes": ["outage", "outage"],
            },
            "unique",
        ),
        (
            "retrieve_knowledge",
            {"query": "x" * 201, "confidence": 0.8},
            "200",
        ),
        (
            "recommend_action",
            {
                "recommendation": "review",
                "confidence": 0.8,
                "requires_human": "yes",
            },
            "boolean",
        ),
    ],
)
def test_invalid_outputs_fail_closed(
    capability: str,
    output: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(AgentOutputViolation, match=message):
        validate_model_output(capability, output)


def test_unknown_capability_and_non_object_output_are_rejected() -> None:
    with pytest.raises(AgentOutputViolation, match="no model-output contract"):
        validate_model_output("unreviewed", {})
    with pytest.raises(AgentOutputViolation, match="JSON object"):
        validate_model_output("summarize", ["not", "an", "object"])


def test_invalid_retrieval_output_fails_before_tool_request(tmp_path: Path) -> None:
    class InvalidModel:
        def complete(self, capability: str, payload: dict[str, Any]) -> dict[str, Any]:
            return {"query": "", "confidence": 0.9}

    repository, desk = setup_job(tmp_path, "retrieve_knowledge")
    tools = ToolRegistry()
    tools.register(KnowledgeSearchTool({"kb-1": "Account recovery runbook"}))

    result = AgentWorker(repository, desk, InvalidModel(), tools, worker_id="worker").run_once()

    assert result is not None
    assert result.job.state is AgentJobState.FAILED
    assert result.job.error == "agent_output_invalid"
    assert result.tool_calls == ()
    with repository.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 0


def test_raw_model_exception_text_is_not_persisted(tmp_path: Path) -> None:
    class FailingModel:
        def complete(self, capability: str, payload: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("provider response included private customer detail")

    repository, desk = setup_job(tmp_path, "summarize")
    result = AgentWorker(
        repository,
        desk,
        FailingModel(),
        ToolRegistry(),
        worker_id="worker",
    ).run_once()

    assert result is not None
    assert result.job.state is AgentJobState.FAILED
    assert result.job.error == "agent_execution_failed"
    assert "private customer detail" not in result.job.error


def test_tool_policy_and_timeout_failures_have_stable_codes() -> None:
    assert failure_code(ToolPolicyViolation("raw details")) == "tool_policy_violation"
    assert failure_code(TimeoutError("provider details")) == "dependency_timeout"
