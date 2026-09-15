from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from service_desk.agent import AgentWorker, DeterministicAgentModel
from service_desk.models import Actor, ActorRole, IssuePriority
from service_desk.observability import collect_snapshot
from service_desk.repository import SQLiteServiceDeskRepository
from service_desk.service import ServiceDesk
from service_desk.tools import KnowledgeSearchTool, ReadOnlySQLTool, ToolContext, ToolRegistry


def generate_evidence() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="agentic-service-desk-") as directory:
        repository = SQLiteServiceDeskRepository(Path(directory) / "service-desk.db")
        desk = ServiceDesk(repository)
        desk.bootstrap()
        project = desk.create_project(key="OPS", name="AI Operations", project_id="project-1")
        requester = Actor("requester-17", ActorRole.REQUESTER)
        human_agent = Actor("agent-4", ActorRole.AGENT)
        desk.configure_sla(
            project_id=project.project_id,
            priority=IssuePriority.HIGH,
            first_response_seconds=900,
            resolution_seconds=14_400,
            actor=Actor("service-admin", ActorRole.ADMIN),
        )
        issue = desk.create_issue(
            project_id=project.project_id,
            summary="Production VPN unavailable",
            description="Authentication fails for the engineering group.",
            priority=IssuePriority.HIGH,
            reporter=requester,
            idempotency_key="demo-request-1",
            labels=("vpn", "production"),
            issue_id="issue-1",
        )
        replay = desk.create_issue(
            project_id=project.project_id,
            summary="Production VPN unavailable",
            description="Authentication fails for the engineering group.",
            priority=IssuePriority.HIGH,
            reporter=requester,
            idempotency_key="demo-request-1",
            labels=("production", "vpn"),
        )
        triage_job = desk.request_agent_job(
            issue.issue_id,
            "triage",
            {"summary": issue.summary, "description": issue.description},
            actor=human_agent,
            job_id="triage-job",
        )
        tools = ToolRegistry()
        tools.register(
            KnowledgeSearchTool(
                {
                    "kb-vpn": "VPN authentication outage: validate identity provider health.",
                    "kb-device": "Device replacement and shipping policy.",
                }
            )
        )
        worker = AgentWorker(
            repository,
            desk,
            DeterministicAgentModel(),
            tools,
            worker_id="worker-1",
        )
        triage_result = worker.run_once()
        triaged = desk.transition(
            issue.issue_id,
            "triage",
            actor=Actor("triage-worker", ActorRole.AI_WORKER),
            expected_version=issue.version,
        )
        assigned = desk.assign(
            issue.issue_id,
            "agent-4",
            actor=Actor("team-lead", ActorRole.ADMIN),
            expected_version=triaged.version,
        )
        started = desk.transition(
            issue.issue_id,
            "start",
            actor=human_agent,
            expected_version=assigned.version,
        )
        desk.comment(
            issue.issue_id,
            "We are investigating the identity provider path.",
            actor=human_agent,
        )
        desk.comment(
            issue.issue_id,
            "Identity provider health check requested.",
            actor=human_agent,
            internal=True,
        )
        resolved = desk.transition(
            issue.issue_id,
            "resolve",
            actor=human_agent,
            expected_version=started.version,
            resolution="Identity provider session keys rotated; requester confirmed access.",
        )
        sla = desk.sla_state(issue.issue_id)
        search = repository.search_issues(
            project.project_id,
            'priority = high AND label = vpn AND text ~ "authentication"',
        )
        sql_result = ReadOnlySQLTool(repository.path).execute(
            {
                "sql": "SELECT issue_key, status, assignee_id FROM issues WHERE project_id = :id",
                "parameters": {"id": project.project_id},
            },
            ToolContext("worker-1", triage_job.job_id),
        )
        outbox = repository.pending_outbox("publisher-1", 100)
        for event in outbox:
            repository.acknowledge_outbox(event.event_id, "publisher-1")
        audit = repository.audit_stream(issue.issue_id)
        operations = collect_snapshot(repository)
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "project": {"key": project.key, "workflow": project.workflow_id},
            "issue": {
                "key": resolved.issue_key,
                "status": resolved.status.value,
                "version": resolved.version,
                "assignee_id": resolved.assignee_id,
            },
            "agent_job": {
                "job_id": triage_result.job.job_id if triage_result else None,
                "state": triage_result.job.state.value if triage_result else None,
                "output": triage_result.job.output_payload if triage_result else None,
            },
            "sql_read_model": sql_result,
            "outbox": {"published_count": len(outbox)},
            "search": {
                "result_keys": [item.issue_key for item in search.items],
                "next_cursor": search.next_cursor,
            },
            "sla": {
                "first_response_breached": sla.first_response_breached,
                "resolution_breached": sla.resolution_breached,
                "first_responded_at": sla.first_responded_at.isoformat()
                if sla.first_responded_at
                else None,
            },
            "audit_event_types": [event["event_type"] for event in audit],
            "operations": {
                "issues_by_status": operations.issues_by_status,
                "agent_jobs_by_state": operations.agent_jobs_by_state,
                "outbox_pending": operations.outbox_pending,
                "sla_resolution_breached": operations.sla_resolution_breached,
            },
            "invariants": {
                "idempotent_create_replayed": replay.issue_id == issue.issue_id,
                "workflow_reached_resolution": resolved.status.value == "resolved",
                "agent_job_durable": triage_result is not None
                and triage_result.job.state.value == "succeeded",
                "outbox_drained": repository.pending_outbox("publisher-2", 10) == [],
                "sql_tool_is_bounded": sql_result["row_count"] == 1,
                "audit_attributed": all(event["actor_id"] for event in audit),
                "safe_search_found_issue": [item.issue_id for item in search.items]
                == [issue.issue_id],
                "sla_first_response_recorded": sla.first_responded_at is not None,
                "operational_snapshot_matches_state": operations.issues_by_status == {"resolved": 1}
                and operations.outbox_pending == 0,
            },
        }


def main() -> None:
    print(json.dumps(generate_evidence(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
