from __future__ import annotations

from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query
from pydantic import BaseModel, Field

from service_desk.models import Actor, ActorRole, IssueLinkType, IssuePriority, IssueStatus
from service_desk.service import ServiceDesk

ActorIdHeader = Annotated[str, Header(min_length=1)]
ActorRoleHeader = Annotated[ActorRole, Header()]
IdempotencyHeader = Annotated[str, Header(min_length=1)]


class ProjectRequest(BaseModel):
    key: str = Field(min_length=2, max_length=10)
    name: str = Field(min_length=1, max_length=200)


class IssueRequest(BaseModel):
    project_id: str
    summary: str = Field(min_length=1, max_length=240)
    description: str = Field(default="", max_length=20_000)
    priority: IssuePriority = IssuePriority.MEDIUM
    labels: list[str] = Field(default_factory=list, max_length=50)


class TransitionRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    expected_version: int = Field(ge=1)
    resolution: str | None = Field(default=None, max_length=10_000)


class AssignmentRequest(BaseModel):
    assignee_id: str = Field(min_length=1, max_length=200)
    expected_version: int = Field(ge=1)


class AgentJobRequest(BaseModel):
    capability: str
    input_payload: dict[str, Any] = Field(default_factory=dict)
    max_attempts: int = Field(default=3, ge=1, le=10)


class CommentRequest(BaseModel):
    body: str = Field(min_length=1, max_length=20_000)
    internal: bool = False


class IssueLinkRequest(BaseModel):
    target_issue_id: str = Field(min_length=1)
    link_type: IssueLinkType


class SLAPolicyRequest(BaseModel):
    first_response_seconds: int = Field(ge=60, le=31_536_000)
    resolution_seconds: int = Field(ge=60, le=31_536_000)


def create_service_desk_router(desk: ServiceDesk) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["service-desk"])

    @router.post("/projects", status_code=201)
    def create_project(payload: ProjectRequest) -> dict[str, Any]:
        return asdict(desk.create_project(key=payload.key, name=payload.name))

    @router.post("/issues", status_code=201)
    def create_issue(
        payload: IssueRequest,
        x_actor_id: ActorIdHeader,
        x_actor_role: ActorRoleHeader,
        idempotency_key: IdempotencyHeader,
    ) -> dict[str, Any]:
        issue = desk.create_issue(
            project_id=payload.project_id,
            summary=payload.summary,
            description=payload.description,
            priority=payload.priority,
            labels=tuple(payload.labels),
            reporter=Actor(x_actor_id, x_actor_role),
            idempotency_key=idempotency_key,
        )
        return _issue(issue)

    @router.get("/issues/{issue_id}")
    def get_issue(issue_id: str) -> dict[str, Any]:
        return _issue(desk.repository.get_issue(issue_id))

    @router.get("/projects/{project_id}/issues")
    def list_issues(
        project_id: str,
        status: IssueStatus | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        return [
            _issue(issue)
            for issue in desk.repository.list_issues(project_id, status=status, limit=limit)
        ]

    @router.post("/issues/{issue_id}/transitions")
    def transition_issue(
        issue_id: str,
        payload: TransitionRequest,
        x_actor_id: ActorIdHeader,
        x_actor_role: ActorRoleHeader,
    ) -> dict[str, Any]:
        return _issue(
            desk.transition(
                issue_id,
                payload.name,
                actor=Actor(x_actor_id, x_actor_role),
                expected_version=payload.expected_version,
                resolution=payload.resolution,
            )
        )

    @router.put("/issues/{issue_id}/assignee")
    def assign_issue(
        issue_id: str,
        payload: AssignmentRequest,
        x_actor_id: ActorIdHeader,
        x_actor_role: ActorRoleHeader,
    ) -> dict[str, Any]:
        return _issue(
            desk.assign(
                issue_id,
                payload.assignee_id,
                actor=Actor(x_actor_id, x_actor_role),
                expected_version=payload.expected_version,
            )
        )

    @router.post("/issues/{issue_id}/comments", status_code=201)
    def add_comment(
        issue_id: str,
        payload: CommentRequest,
        x_actor_id: ActorIdHeader,
        x_actor_role: ActorRoleHeader,
    ) -> dict[str, Any]:
        comment = desk.comment(
            issue_id,
            payload.body,
            actor=Actor(x_actor_id, x_actor_role),
            internal=payload.internal,
        )
        return asdict(comment)

    @router.get("/issues/{issue_id}/comments")
    def list_comments(
        issue_id: str,
        x_actor_role: ActorRoleHeader,
    ) -> list[dict[str, Any]]:
        include_internal = x_actor_role in {ActorRole.AGENT, ActorRole.ADMIN}
        return [
            asdict(comment)
            for comment in desk.repository.list_comments(
                issue_id,
                include_internal=include_internal,
            )
        ]

    @router.post("/issues/{issue_id}/agent-jobs", status_code=202)
    def request_agent_job(
        issue_id: str,
        payload: AgentJobRequest,
        x_actor_id: ActorIdHeader,
        x_actor_role: ActorRoleHeader,
    ) -> dict[str, Any]:
        job = desk.request_agent_job(
            issue_id,
            payload.capability,
            payload.input_payload,
            actor=Actor(x_actor_id, x_actor_role),
            max_attempts=payload.max_attempts,
        )
        result = asdict(job)
        result["state"] = job.state.value
        return result

    @router.get("/issues/{issue_id}/audit")
    def audit_stream(issue_id: str) -> list[dict[str, Any]]:
        return desk.repository.audit_stream(issue_id)

    @router.put("/projects/{project_id}/sla/{priority}")
    def configure_sla(
        project_id: str,
        priority: IssuePriority,
        payload: SLAPolicyRequest,
        x_actor_id: ActorIdHeader,
        x_actor_role: ActorRoleHeader,
    ) -> dict[str, Any]:
        return asdict(
            desk.configure_sla(
                project_id=project_id,
                priority=priority,
                first_response_seconds=payload.first_response_seconds,
                resolution_seconds=payload.resolution_seconds,
                actor=Actor(x_actor_id, x_actor_role),
            )
        )

    @router.get("/issues/{issue_id}/sla")
    def get_sla(issue_id: str) -> dict[str, Any]:
        return asdict(desk.sla_state(issue_id))

    @router.post("/issues/{source_issue_id}/links", status_code=201)
    def create_issue_link(
        source_issue_id: str,
        payload: IssueLinkRequest,
        x_actor_id: ActorIdHeader,
        x_actor_role: ActorRoleHeader,
    ) -> dict[str, Any]:
        link = desk.link_issues(
            source_issue_id=source_issue_id,
            target_issue_id=payload.target_issue_id,
            link_type=payload.link_type,
            actor=Actor(x_actor_id, x_actor_role),
        )
        result = asdict(link)
        result["link_type"] = link.link_type.value
        return result

    @router.get("/issues/{issue_id}/links")
    def list_issue_links(issue_id: str) -> list[dict[str, Any]]:
        values = []
        for link in desk.repository.list_issue_links(issue_id):
            item = asdict(link)
            item["link_type"] = link.link_type.value
            values.append(item)
        return values

    @router.get("/issues/{issue_id}/descendants")
    def list_descendants(issue_id: str) -> list[dict[str, Any]]:
        return [_issue(issue) for issue in desk.repository.issue_descendants(issue_id)]

    @router.post("/tool-calls/{call_id}/approval")
    def approve_tool_call(
        call_id: str,
        x_actor_id: ActorIdHeader,
        x_actor_role: ActorRoleHeader,
    ) -> dict[str, Any]:
        call = desk.approve_tool_call(call_id, actor=Actor(x_actor_id, x_actor_role))
        result = asdict(call)
        result["risk"] = call.risk.value
        return result

    return router


def _issue(issue) -> dict[str, Any]:
    result = asdict(issue)
    result["status"] = issue.status.value
    result["priority"] = issue.priority.value
    return result
