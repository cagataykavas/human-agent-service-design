from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from service_desk.errors import Forbidden, InvalidTransition
from service_desk.models import (
    Actor,
    ActorRole,
    AgentJob,
    AgentJobState,
    Comment,
    Issue,
    IssueLink,
    IssueLinkType,
    IssuePriority,
    Project,
    SLAPolicy,
    SLAState,
    ToolCall,
    ToolRisk,
    WorkflowDefinition,
    default_workflow,
)
from service_desk.repository import SQLiteServiceDeskRepository


def _hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


class ServiceDesk:
    """Application boundary for issue workflow and durable agent execution."""

    def __init__(
        self,
        repository: SQLiteServiceDeskRepository,
        *,
        tool_risks: dict[str, ToolRisk] | None = None,
    ) -> None:
        self.repository = repository
        self.tool_risks = tool_risks or {
            "knowledge_search": ToolRisk.READ_ONLY,
            "service_desk_sql": ToolRisk.READ_ONLY,
            "assign_issue": ToolRisk.REVERSIBLE_WRITE,
            "transition_issue": ToolRisk.HIGH_IMPACT,
        }

    def bootstrap(self, workflow: WorkflowDefinition | None = None) -> WorkflowDefinition:
        selected = workflow or default_workflow()
        return self.repository.register_workflow(selected)

    def create_project(
        self,
        *,
        key: str,
        name: str,
        workflow_id: str = "service-request",
        workflow_version: int = 1,
        project_id: str | None = None,
    ) -> Project:
        normalized_key = key.strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9]{1,9}", normalized_key):
            raise ValueError("project key must contain 2-10 uppercase letters or digits")
        if not name.strip():
            raise ValueError("project name must not be empty")
        return self.repository.create_project(
            Project(
                project_id=project_id or str(uuid4()),
                key=normalized_key,
                name=name.strip(),
                workflow_id=workflow_id,
                workflow_version=workflow_version,
            )
        )

    def create_issue(
        self,
        *,
        project_id: str,
        summary: str,
        description: str,
        priority: IssuePriority,
        reporter: Actor,
        idempotency_key: str,
        labels: tuple[str, ...] = (),
        issue_id: str | None = None,
    ) -> Issue:
        if reporter.role not in {ActorRole.REQUESTER, ActorRole.AGENT, ActorRole.ADMIN}:
            raise Forbidden("AI workers cannot create user-authored issues")
        if not summary.strip() or len(summary) > 240:
            raise ValueError("summary must contain 1-240 characters")
        if not idempotency_key.strip():
            raise ValueError("idempotency_key must not be empty")
        normalized_labels = tuple(
            sorted({label.strip().lower() for label in labels if label.strip()})
        )
        payload = {
            "project_id": project_id,
            "summary": summary.strip(),
            "description": description.strip(),
            "priority": priority.value,
            "reporter_id": reporter.actor_id,
            "labels": normalized_labels,
        }
        return self.repository.create_issue(
            issue_id=issue_id or str(uuid4()),
            project_id=project_id,
            summary=payload["summary"],
            description=payload["description"],
            priority=priority,
            reporter_id=reporter.actor_id,
            labels=normalized_labels,
            idempotency_key=idempotency_key,
            request_hash=_hash(payload),
            outbox_event_id=str(uuid4()),
        )

    def transition(
        self,
        issue_id: str,
        transition_name: str,
        *,
        actor: Actor,
        expected_version: int,
        resolution: str | None = None,
    ) -> Issue:
        issue = self.repository.get_issue(issue_id)
        project = self.repository.get_project(issue.project_id)
        workflow = self.repository.get_workflow(project.workflow_id, project.workflow_version)
        rule = workflow.resolve(issue.status, transition_name, actor.role)
        if rule.requires_resolution and not (resolution and resolution.strip()):
            raise InvalidTransition("resolution text is required for this transition")
        if not rule.requires_resolution and resolution is not None:
            raise InvalidTransition("resolution is accepted only by a resolving transition")
        return self.repository.transition_issue(
            issue_id=issue_id,
            expected_version=expected_version,
            target=rule.target,
            actor_id=actor.actor_id,
            transition_name=rule.name,
            resolution=resolution.strip() if resolution else None,
            outbox_event_id=str(uuid4()),
        )

    def assign(
        self,
        issue_id: str,
        assignee_id: str,
        *,
        actor: Actor,
        expected_version: int,
    ) -> Issue:
        if actor.role not in {ActorRole.AGENT, ActorRole.ADMIN}:
            raise Forbidden("only agents and admins can assign issues")
        if not assignee_id.strip():
            raise ValueError("assignee_id must not be empty")
        return self.repository.assign_issue(
            issue_id,
            assignee_id,
            expected_version,
            actor.actor_id,
            str(uuid4()),
        )

    def comment(
        self,
        issue_id: str,
        body: str,
        *,
        actor: Actor,
        internal: bool = False,
        comment_id: str | None = None,
    ) -> Comment:
        if not body.strip() or len(body) > 20_000:
            raise ValueError("comment must contain 1-20,000 characters")
        if internal and actor.role not in {ActorRole.AGENT, ActorRole.ADMIN}:
            raise Forbidden("requesters cannot create internal comments")
        return self.repository.add_comment(
            comment_id=comment_id or str(uuid4()),
            issue_id=issue_id,
            author_id=actor.actor_id,
            body=body.strip(),
            internal=internal,
            event_id=str(uuid4()),
            mark_first_response=not internal and actor.role in {ActorRole.AGENT, ActorRole.ADMIN},
        )

    def configure_sla(
        self,
        *,
        project_id: str,
        priority: IssuePriority,
        first_response_seconds: int,
        resolution_seconds: int,
        actor: Actor,
    ) -> SLAPolicy:
        if actor.role is not ActorRole.ADMIN:
            raise Forbidden("only admins can configure project SLA policy")
        return self.repository.configure_sla_policy(
            SLAPolicy(
                project_id=project_id,
                priority=priority,
                first_response_seconds=first_response_seconds,
                resolution_seconds=resolution_seconds,
            )
        )

    def sla_state(self, issue_id: str) -> SLAState:
        return self.repository.get_sla_state(issue_id)

    def link_issues(
        self,
        *,
        source_issue_id: str,
        target_issue_id: str,
        link_type: IssueLinkType,
        actor: Actor,
        link_id: str | None = None,
    ) -> IssueLink:
        if actor.role not in {ActorRole.AGENT, ActorRole.ADMIN}:
            raise Forbidden("only agents and admins can link issues")
        return self.repository.create_issue_link(
            IssueLink(
                link_id=link_id or str(uuid4()),
                source_issue_id=source_issue_id,
                target_issue_id=target_issue_id,
                link_type=link_type,
                created_by=actor.actor_id,
                created_at=self.repository.now(),
            )
        )

    def request_agent_job(
        self,
        issue_id: str,
        capability: str,
        input_payload: dict[str, Any],
        *,
        actor: Actor,
        max_attempts: int = 3,
        job_id: str | None = None,
    ) -> AgentJob:
        allowed = {"triage", "summarize", "retrieve_knowledge", "recommend_action"}
        if capability not in allowed:
            raise ValueError(f"unsupported capability {capability!r}")
        if actor.role not in {ActorRole.AGENT, ActorRole.ADMIN, ActorRole.AI_WORKER}:
            raise Forbidden("requesters cannot directly schedule agent execution")
        if not 1 <= max_attempts <= 10:
            raise ValueError("max_attempts must be in [1, 10]")
        now = datetime.now(UTC)
        job = AgentJob(
            job_id=job_id or str(uuid4()),
            issue_id=issue_id,
            capability=capability,
            state=AgentJobState.QUEUED,
            input_payload=dict(input_payload),
            output_payload=None,
            error=None,
            attempt=0,
            max_attempts=max_attempts,
            lease_owner=None,
            lease_until=None,
            created_at=now,
            updated_at=now,
        )
        return self.repository.create_agent_job(job, str(uuid4()))

    def request_tool_call(
        self,
        *,
        job_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        requested_by: str,
        call_id: str | None = None,
    ) -> ToolCall:
        try:
            risk = self.tool_risks[tool_name]
        except KeyError as exc:
            raise Forbidden(f"tool {tool_name!r} is not allowlisted") from exc
        state = "ready" if risk is ToolRisk.READ_ONLY else "waiting_for_approval"
        return self.repository.create_tool_call(
            ToolCall(
                call_id=call_id or str(uuid4()),
                job_id=job_id,
                tool_name=tool_name,
                risk=risk,
                arguments=dict(arguments),
                state=state,
                requested_by=requested_by,
                approved_by=None,
                result=None,
            )
        )

    def approve_tool_call(self, call_id: str, *, actor: Actor) -> ToolCall:
        if actor.role not in {ActorRole.AGENT, ActorRole.ADMIN}:
            raise Forbidden("only a human agent or admin can approve write tools")
        return self.repository.approve_tool_call(call_id, actor.actor_id)
