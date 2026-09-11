from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class IssueStatus(StrEnum):
    OPEN = "open"
    TRIAGED = "triaged"
    IN_PROGRESS = "in_progress"
    WAITING_FOR_CUSTOMER = "waiting_for_customer"
    RESOLVED = "resolved"
    CLOSED = "closed"


class IssuePriority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ActorRole(StrEnum):
    REQUESTER = "requester"
    AGENT = "agent"
    ADMIN = "admin"
    AI_WORKER = "ai_worker"


class AgentJobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ToolRisk(StrEnum):
    READ_ONLY = "read_only"
    REVERSIBLE_WRITE = "reversible_write"
    HIGH_IMPACT = "high_impact"


@dataclass(frozen=True, slots=True)
class Actor:
    actor_id: str
    role: ActorRole

    def __post_init__(self) -> None:
        if not self.actor_id.strip():
            raise ValueError("actor_id must not be empty")


@dataclass(frozen=True, slots=True)
class TransitionRule:
    name: str
    source: IssueStatus
    target: IssueStatus
    allowed_roles: frozenset[ActorRole]
    requires_resolution: bool = False


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    workflow_id: str
    version: int
    transitions: tuple[TransitionRule, ...]

    def __post_init__(self) -> None:
        if not self.workflow_id or self.version < 1:
            raise ValueError("workflow_id and positive version are required")
        keys = [(rule.source, rule.name) for rule in self.transitions]
        if len(keys) != len(set(keys)):
            raise ValueError("transition names must be unique within each source state")

    def resolve(self, source: IssueStatus, transition_name: str, role: ActorRole) -> TransitionRule:
        for rule in self.transitions:
            if rule.source is source and rule.name == transition_name:
                if role not in rule.allowed_roles:
                    from service_desk.errors import Forbidden

                    raise Forbidden(
                        f"role {role.value!r} cannot execute transition {transition_name!r}"
                    )
                return rule
        from service_desk.errors import InvalidTransition

        raise InvalidTransition(f"transition {transition_name!r} is invalid from {source.value!r}")


def default_workflow() -> WorkflowDefinition:
    agent_roles = frozenset({ActorRole.AGENT, ActorRole.ADMIN})
    worker_roles = frozenset({ActorRole.AGENT, ActorRole.ADMIN, ActorRole.AI_WORKER})
    return WorkflowDefinition(
        workflow_id="service-request",
        version=1,
        transitions=(
            TransitionRule("triage", IssueStatus.OPEN, IssueStatus.TRIAGED, worker_roles),
            TransitionRule("start", IssueStatus.TRIAGED, IssueStatus.IN_PROGRESS, agent_roles),
            TransitionRule(
                "request_information",
                IssueStatus.IN_PROGRESS,
                IssueStatus.WAITING_FOR_CUSTOMER,
                agent_roles,
            ),
            TransitionRule(
                "customer_replied",
                IssueStatus.WAITING_FOR_CUSTOMER,
                IssueStatus.IN_PROGRESS,
                frozenset({ActorRole.REQUESTER, ActorRole.AGENT, ActorRole.ADMIN}),
            ),
            TransitionRule(
                "resolve",
                IssueStatus.IN_PROGRESS,
                IssueStatus.RESOLVED,
                agent_roles,
                requires_resolution=True,
            ),
            TransitionRule(
                "close",
                IssueStatus.RESOLVED,
                IssueStatus.CLOSED,
                frozenset({ActorRole.REQUESTER, ActorRole.AGENT, ActorRole.ADMIN}),
            ),
            TransitionRule(
                "reopen",
                IssueStatus.RESOLVED,
                IssueStatus.IN_PROGRESS,
                frozenset({ActorRole.REQUESTER, ActorRole.AGENT, ActorRole.ADMIN}),
            ),
        ),
    )


@dataclass(frozen=True, slots=True)
class Project:
    project_id: str
    key: str
    name: str
    workflow_id: str
    workflow_version: int
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class Issue:
    issue_id: str
    issue_key: str
    project_id: str
    sequence: int
    summary: str
    description: str
    status: IssueStatus
    priority: IssuePriority
    reporter_id: str
    assignee_id: str | None
    resolution: str | None
    version: int
    created_at: datetime
    updated_at: datetime
    labels: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Comment:
    comment_id: str
    issue_id: str
    author_id: str
    body: str
    internal: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class OutboxEvent:
    event_id: str
    aggregate_id: str
    event_type: str
    payload: dict[str, Any]
    attempts: int
    available_at: datetime


@dataclass(frozen=True, slots=True)
class AgentJob:
    job_id: str
    issue_id: str
    capability: str
    state: AgentJobState
    input_payload: dict[str, Any]
    output_payload: dict[str, Any] | None
    error: str | None
    attempt: int
    max_attempts: int
    lease_owner: str | None
    lease_until: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ToolCall:
    call_id: str
    job_id: str
    tool_name: str
    risk: ToolRisk
    arguments: dict[str, Any]
    state: str
    requested_by: str
    approved_by: str | None
    result: dict[str, Any] | None
