from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from service_desk.errors import Conflict, NotFound
from service_desk.models import (
    AgentJob,
    AgentJobState,
    Comment,
    Issue,
    IssueLink,
    IssueLinkType,
    IssuePriority,
    IssueSearchPage,
    IssueStatus,
    OutboxEvent,
    Project,
    SLAPolicy,
    SLAState,
    ToolCall,
    ToolRisk,
    TransitionRule,
    WorkflowDefinition,
)
from service_desk.search import IssueCursor, compile_issue_query

SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS workflows (
    workflow_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    definition_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(workflow_id, version)
);

CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    project_key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    workflow_version INTEGER NOT NULL,
    next_issue_sequence INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    FOREIGN KEY(workflow_id, workflow_version) REFERENCES workflows(workflow_id, version)
);

CREATE TABLE IF NOT EXISTS issues (
    issue_id TEXT PRIMARY KEY,
    issue_key TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL,
    issue_sequence INTEGER NOT NULL,
    summary TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL,
    priority TEXT NOT NULL,
    reporter_id TEXT NOT NULL,
    assignee_id TEXT,
    resolution TEXT,
    version INTEGER NOT NULL,
    labels_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(project_id),
    UNIQUE(project_id, issue_sequence)
);
CREATE INDEX IF NOT EXISTS idx_issues_queue
    ON issues(project_id, status, priority, updated_at);

CREATE TABLE IF NOT EXISTS issue_links (
    link_id TEXT PRIMARY KEY,
    source_issue_id TEXT NOT NULL,
    target_issue_id TEXT NOT NULL,
    link_type TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(source_issue_id) REFERENCES issues(issue_id),
    FOREIGN KEY(target_issue_id) REFERENCES issues(issue_id),
    UNIQUE(source_issue_id, target_issue_id, link_type),
    CHECK(source_issue_id <> target_issue_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_issue_single_parent
    ON issue_links(target_issue_id) WHERE link_type = 'parent_of';
CREATE INDEX IF NOT EXISTS idx_issue_links_source
    ON issue_links(source_issue_id, link_type, target_issue_id);
CREATE INDEX IF NOT EXISTS idx_issue_links_target
    ON issue_links(target_issue_id, link_type, source_issue_id);

CREATE TABLE IF NOT EXISTS sla_policies (
    project_id TEXT NOT NULL,
    priority TEXT NOT NULL,
    first_response_seconds INTEGER NOT NULL CHECK(first_response_seconds >= 60),
    resolution_seconds INTEGER NOT NULL CHECK(resolution_seconds >= first_response_seconds),
    PRIMARY KEY(project_id, priority),
    FOREIGN KEY(project_id) REFERENCES projects(project_id)
);

CREATE TABLE IF NOT EXISTS sla_instances (
    issue_id TEXT PRIMARY KEY,
    first_response_due_at TEXT NOT NULL,
    resolution_due_at TEXT NOT NULL,
    paused_at TEXT,
    total_paused_seconds INTEGER NOT NULL DEFAULT 0,
    first_responded_at TEXT,
    resolved_at TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(issue_id) REFERENCES issues(issue_id)
);
CREATE INDEX IF NOT EXISTS idx_sla_resolution_queue
    ON sla_instances(resolved_at, resolution_due_at);

CREATE TABLE IF NOT EXISTS comments (
    comment_id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL,
    author_id TEXT NOT NULL,
    body TEXT NOT NULL,
    internal INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(issue_id) REFERENCES issues(issue_id)
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    FOREIGN KEY(issue_id) REFERENCES issues(issue_id)
);

CREATE TABLE IF NOT EXISTS outbox_events (
    event_id TEXT PRIMARY KEY,
    aggregate_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    leased_by TEXT,
    lease_until TEXT,
    published_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outbox_dispatch
    ON outbox_events(published_at, available_at, lease_until);

CREATE TABLE IF NOT EXISTS agent_jobs (
    job_id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL,
    capability TEXT NOT NULL,
    state TEXT NOT NULL,
    input_json TEXT NOT NULL,
    output_json TEXT,
    error TEXT,
    attempt INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    lease_owner TEXT,
    lease_until TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(issue_id) REFERENCES issues(issue_id)
);
CREATE INDEX IF NOT EXISTS idx_agent_jobs_claim
    ON agent_jobs(state, lease_until, created_at);

CREATE TABLE IF NOT EXISTS tool_calls (
    call_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    risk TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    state TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    approved_by TEXT,
    result_json TEXT,
    request_digest TEXT,
    approved_at TEXT,
    approval_expires_at TEXT,
    execution_owner TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(job_id) REFERENCES agent_jobs(job_id)
);
"""


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class SQLiteServiceDeskRepository:
    """SQLite adapter preserving workflow, outbox and agent-job transactions."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = str(path)
        self.clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            self._migrate_tool_calls(connection)

    @staticmethod
    def _migrate_tool_calls(connection: sqlite3.Connection) -> None:
        """Add approval-integrity fields without rewriting existing local databases."""
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(tool_calls)").fetchall()
        }
        additions = {
            "request_digest": "TEXT",
            "approved_at": "TEXT",
            "approval_expires_at": "TEXT",
            "execution_owner": "TEXT",
        }
        for name, data_type in additions.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE tool_calls ADD COLUMN {name} {data_type}")

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    @staticmethod
    def time(value: datetime) -> str:
        if value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return value.astimezone(UTC).isoformat()

    def register_workflow(self, workflow: WorkflowDefinition) -> WorkflowDefinition:
        payload = {
            "workflow_id": workflow.workflow_id,
            "version": workflow.version,
            "transitions": [
                {
                    "name": rule.name,
                    "source": rule.source.value,
                    "target": rule.target.value,
                    "allowed_roles": sorted(role.value for role in rule.allowed_roles),
                    "requires_resolution": rule.requires_resolution,
                }
                for rule in workflow.transitions
            ],
        }
        encoded = _dump(payload)
        with self._lock, self.connect() as connection:
            existing = connection.execute(
                "SELECT definition_json FROM workflows WHERE workflow_id = ? AND version = ?",
                (workflow.workflow_id, workflow.version),
            ).fetchone()
            if existing and existing["definition_json"] != encoded:
                raise Conflict("a workflow version is immutable once registered")
            connection.execute(
                """
                INSERT OR IGNORE INTO workflows(workflow_id, version, definition_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (workflow.workflow_id, workflow.version, encoded, self.time(self.now())),
            )
        return workflow

    def get_workflow(self, workflow_id: str, version: int) -> WorkflowDefinition:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT definition_json FROM workflows WHERE workflow_id = ? AND version = ?",
                (workflow_id, version),
            ).fetchone()
        if not row:
            raise NotFound(f"workflow {workflow_id}:{version} not found")
        payload = json.loads(row["definition_json"])
        from service_desk.models import ActorRole

        return WorkflowDefinition(
            workflow_id=payload["workflow_id"],
            version=payload["version"],
            transitions=tuple(
                TransitionRule(
                    name=item["name"],
                    source=IssueStatus(item["source"]),
                    target=IssueStatus(item["target"]),
                    allowed_roles=frozenset(ActorRole(role) for role in item["allowed_roles"]),
                    requires_resolution=item["requires_resolution"],
                )
                for item in payload["transitions"]
            ),
        )

    def create_project(self, project: Project) -> Project:
        self.get_workflow(project.workflow_id, project.workflow_version)
        with self._lock, self.connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO projects(
                        project_id, project_key, name, workflow_id, workflow_version, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project.project_id,
                        project.key,
                        project.name,
                        project.workflow_id,
                        project.workflow_version,
                        self.time(project.created_at),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict(f"project id or key already exists: {project.key}") from exc
        return project

    def get_project(self, project_id: str) -> Project:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
        if not row:
            raise NotFound(f"project {project_id!r} not found")
        return Project(
            project_id=row["project_id"],
            key=row["project_key"],
            name=row["name"],
            workflow_id=row["workflow_id"],
            workflow_version=row["workflow_version"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def create_issue(
        self,
        *,
        issue_id: str,
        project_id: str,
        summary: str,
        description: str,
        priority: IssuePriority,
        reporter_id: str,
        labels: tuple[str, ...],
        idempotency_key: str,
        request_hash: str,
        outbox_event_id: str,
    ) -> Issue:
        now = self.now()
        scope = f"create-issue:{project_id}"
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = connection.execute(
                """
                SELECT request_hash, resource_id FROM idempotency_keys
                WHERE scope = ? AND idempotency_key = ?
                """,
                (scope, idempotency_key),
            ).fetchone()
            if replay:
                if replay["request_hash"] != request_hash:
                    raise Conflict("idempotency key was reused with a different request")
                issue = self._get_issue(connection, replay["resource_id"])
                connection.commit()
                return issue
            project = connection.execute(
                "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            if not project:
                raise NotFound(f"project {project_id!r} not found")
            sequence = int(project["next_issue_sequence"])
            issue_key = f"{project['project_key']}-{sequence}"
            connection.execute(
                "UPDATE projects SET next_issue_sequence = ? WHERE project_id = ?",
                (sequence + 1, project_id),
            )
            connection.execute(
                """
                INSERT INTO issues(
                    issue_id, issue_key, project_id, issue_sequence, summary, description,
                    status, priority, reporter_id, assignee_id, resolution, version,
                    labels_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 1, ?, ?, ?)
                """,
                (
                    issue_id,
                    issue_key,
                    project_id,
                    sequence,
                    summary,
                    description,
                    IssueStatus.OPEN.value,
                    priority.value,
                    reporter_id,
                    _dump(labels),
                    self.time(now),
                    self.time(now),
                ),
            )
            policy = connection.execute(
                """
                SELECT first_response_seconds, resolution_seconds
                FROM sla_policies WHERE project_id = ? AND priority = ?
                """,
                (project_id, priority.value),
            ).fetchone()
            if policy:
                connection.execute(
                    """
                    INSERT INTO sla_instances(
                        issue_id, first_response_due_at, resolution_due_at, updated_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        issue_id,
                        self.time(now + timedelta(seconds=policy["first_response_seconds"])),
                        self.time(now + timedelta(seconds=policy["resolution_seconds"])),
                        self.time(now),
                    ),
                )
            connection.execute(
                "INSERT INTO idempotency_keys VALUES (?, ?, ?, ?, ?)",
                (scope, idempotency_key, request_hash, issue_id, self.time(now)),
            )
            self._audit(
                connection, outbox_event_id + "-audit", issue_id, "issue_created", reporter_id, {}
            )
            self._outbox(
                connection,
                outbox_event_id,
                issue_id,
                "issue.created",
                {"issue_id": issue_id, "issue_key": issue_key, "project_id": project_id},
            )
            issue = self._get_issue(connection, issue_id)
            connection.commit()
            return issue

    def get_issue(self, issue_id: str) -> Issue:
        with self.connect() as connection:
            return self._get_issue(connection, issue_id)

    def list_issues(
        self,
        project_id: str,
        *,
        status: IssueStatus | None = None,
        limit: int = 100,
    ) -> list[Issue]:
        query = "SELECT * FROM issues WHERE project_id = ?"
        params: list[Any] = [project_id]
        if status is not None:
            query += " AND status = ?"
            params.append(status.value)
        query += " ORDER BY updated_at DESC, issue_sequence DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._issue(row) for row in rows]

    def search_issues(
        self,
        project_id: str,
        expression: str | None,
        *,
        cursor: str | None = None,
        limit: int = 50,
        risk_horizon_seconds: int = 1800,
    ) -> IssueSearchPage:
        """Compile an allowlisted query and paginate on a stable compound sort key."""
        self.get_project(project_id)
        if not 1 <= limit <= 100:
            raise ValueError("search limit must be in [1, 100]")
        if not 60 <= risk_horizon_seconds <= 86_400:
            raise ValueError("risk horizon must be in [60, 86400] seconds")
        compiled = compile_issue_query(expression)
        now = self.now()
        risk_horizon = now + timedelta(seconds=risk_horizon_seconds)
        parameters: list[Any] = [project_id]
        query = (
            "SELECT issues.* FROM issues "
            "LEFT JOIN sla_instances AS sla ON sla.issue_id = issues.issue_id "
            "WHERE issues.project_id = ?"
        )
        if compiled.where_sql:
            query += " AND " + compiled.where_sql
            parameters.extend(
                self.time(now)
                if value == "__NOW__"
                else self.time(risk_horizon)
                if value == "__RISK_HORIZON__"
                else value
                for value in compiled.parameters
            )
        if cursor:
            position = IssueCursor.decode(cursor)
            query += " AND (issues.updated_at < ? OR (issues.updated_at = ? AND issues.issue_sequence < ?))"
            parameters.extend((position.updated_at, position.updated_at, position.sequence))
        query += " ORDER BY issues.updated_at DESC, issues.issue_sequence DESC LIMIT ?"
        parameters.append(limit + 1)
        with self.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        has_more = len(rows) > limit
        selected = rows[:limit]
        next_cursor = None
        if has_more and selected:
            last = selected[-1]
            next_cursor = IssueCursor(last["updated_at"], last["issue_sequence"]).encode()
        return IssueSearchPage(tuple(self._issue(row) for row in selected), next_cursor)

    def transition_issue(
        self,
        *,
        issue_id: str,
        expected_version: int,
        target: IssueStatus,
        actor_id: str,
        transition_name: str,
        resolution: str | None,
        outbox_event_id: str,
    ) -> Issue:
        now = self.now()
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._get_issue(connection, issue_id)
            if current.version != expected_version:
                raise Conflict(
                    f"expected issue version {expected_version}, current version is {current.version}"
                )
            cursor = connection.execute(
                """
                UPDATE issues SET status = ?, resolution = ?, version = version + 1, updated_at = ?
                WHERE issue_id = ? AND version = ?
                """,
                (target.value, resolution, self.time(now), issue_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise Conflict("concurrent issue update detected")
            payload = {
                "transition": transition_name,
                "from": current.status.value,
                "to": target.value,
                "version": expected_version + 1,
            }
            self._audit(
                connection,
                outbox_event_id + "-audit",
                issue_id,
                "issue_transitioned",
                actor_id,
                payload,
            )
            self._outbox(connection, outbox_event_id, issue_id, "issue.transitioned", payload)
            self._update_sla_for_transition(connection, current.status, target, issue_id, now)
            updated = self._get_issue(connection, issue_id)
            connection.commit()
            return updated

    def assign_issue(
        self, issue_id: str, assignee_id: str, expected_version: int, actor_id: str, event_id: str
    ) -> Issue:
        now = self.now()
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._get_issue(connection, issue_id)
            if current.version != expected_version:
                raise Conflict("stale issue version")
            connection.execute(
                """
                UPDATE issues SET assignee_id = ?, version = version + 1, updated_at = ?
                WHERE issue_id = ? AND version = ?
                """,
                (assignee_id, self.time(now), issue_id, expected_version),
            )
            payload = {"assignee_id": assignee_id, "version": expected_version + 1}
            self._audit(
                connection, event_id + "-audit", issue_id, "issue_assigned", actor_id, payload
            )
            self._outbox(connection, event_id, issue_id, "issue.assigned", payload)
            updated = self._get_issue(connection, issue_id)
            connection.commit()
            return updated

    def add_comment(
        self,
        *,
        comment_id: str,
        issue_id: str,
        author_id: str,
        body: str,
        internal: bool,
        event_id: str,
        mark_first_response: bool = False,
    ) -> Comment:
        now = self.now()
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._get_issue(connection, issue_id)
            connection.execute(
                "INSERT INTO comments VALUES (?, ?, ?, ?, ?, ?)",
                (comment_id, issue_id, author_id, body, int(internal), self.time(now)),
            )
            payload = {"comment_id": comment_id, "internal": internal}
            self._audit(
                connection,
                event_id + "-audit",
                issue_id,
                "comment_added",
                author_id,
                payload,
            )
            self._outbox(connection, event_id, issue_id, "issue.comment_added", payload)
            if mark_first_response:
                connection.execute(
                    """
                    UPDATE sla_instances SET first_responded_at = COALESCE(first_responded_at, ?),
                        updated_at = ? WHERE issue_id = ?
                    """,
                    (self.time(now), self.time(now), issue_id),
                )
            connection.commit()
        return Comment(comment_id, issue_id, author_id, body, internal, now)

    def list_comments(self, issue_id: str, *, include_internal: bool) -> list[Comment]:
        query = "SELECT * FROM comments WHERE issue_id = ?"
        if not include_internal:
            query += " AND internal = 0"
        query += " ORDER BY created_at, comment_id"
        with self.connect() as connection:
            self._get_issue(connection, issue_id)
            rows = connection.execute(query, (issue_id,)).fetchall()
        return [
            Comment(
                comment_id=row["comment_id"],
                issue_id=row["issue_id"],
                author_id=row["author_id"],
                body=row["body"],
                internal=bool(row["internal"]),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def pending_outbox(self, owner: str, limit: int, lease_seconds: int = 30) -> list[OutboxEvent]:
        now = self.now()
        lease_until = now + timedelta(seconds=lease_seconds)
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT * FROM outbox_events
                WHERE published_at IS NULL AND available_at <= ?
                  AND (lease_until IS NULL OR lease_until <= ?)
                ORDER BY created_at LIMIT ?
                """,
                (self.time(now), self.time(now), max(1, min(limit, 500))),
            ).fetchall()
            ids = [row["event_id"] for row in rows]
            for event_id in ids:
                connection.execute(
                    "UPDATE outbox_events SET leased_by = ?, lease_until = ? WHERE event_id = ?",
                    (owner, self.time(lease_until), event_id),
                )
            connection.commit()
        return [
            OutboxEvent(
                event_id=row["event_id"],
                aggregate_id=row["aggregate_id"],
                event_type=row["event_type"],
                payload=json.loads(row["payload_json"]),
                attempts=row["attempts"],
                available_at=datetime.fromisoformat(row["available_at"]),
            )
            for row in rows
        ]

    def acknowledge_outbox(self, event_id: str, owner: str) -> None:
        with self._lock, self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE outbox_events SET published_at = ?, leased_by = NULL, lease_until = NULL
                WHERE event_id = ? AND leased_by = ? AND published_at IS NULL
                """,
                (self.time(self.now()), event_id, owner),
            )
            if cursor.rowcount != 1:
                raise Conflict("outbox acknowledgement requires the active lease")

    def reject_outbox(self, event_id: str, owner: str, error: str, retry_delay: int) -> None:
        now = self.now()
        with self._lock, self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE outbox_events SET attempts = attempts + 1, last_error = ?,
                    available_at = ?, leased_by = NULL, lease_until = NULL
                WHERE event_id = ? AND leased_by = ? AND published_at IS NULL
                """,
                (error[:1000], self.time(now + timedelta(seconds=retry_delay)), event_id, owner),
            )
            if cursor.rowcount != 1:
                raise Conflict("outbox retry requires the active lease")

    def create_agent_job(self, job: AgentJob, event_id: str) -> AgentJob:
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._get_issue(connection, job.issue_id)
            connection.execute(
                """
                INSERT INTO agent_jobs(
                    job_id, issue_id, capability, state, input_json, output_json, error,
                    attempt, max_attempts, lease_owner, lease_until, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL, 0, ?, NULL, NULL, ?, ?)
                """,
                (
                    job.job_id,
                    job.issue_id,
                    job.capability,
                    job.state.value,
                    _dump(job.input_payload),
                    job.max_attempts,
                    self.time(job.created_at),
                    self.time(job.updated_at),
                ),
            )
            self._outbox(
                connection,
                event_id,
                job.issue_id,
                "agent.job.requested",
                {"job_id": job.job_id, "capability": job.capability},
            )
            connection.commit()
        return job

    def claim_agent_job(self, owner: str, lease_seconds: int = 60) -> AgentJob | None:
        now = self.now()
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM agent_jobs
                WHERE state IN (?, ?) AND attempt < max_attempts
                  AND (lease_until IS NULL OR lease_until <= ?)
                ORDER BY created_at LIMIT 1
                """,
                (AgentJobState.QUEUED.value, AgentJobState.RUNNING.value, self.time(now)),
            ).fetchone()
            if not row:
                connection.commit()
                return None
            connection.execute(
                """
                UPDATE agent_jobs SET state = ?, lease_owner = ?, lease_until = ?,
                    attempt = attempt + 1, updated_at = ? WHERE job_id = ?
                """,
                (
                    AgentJobState.RUNNING.value,
                    owner,
                    self.time(now + timedelta(seconds=lease_seconds)),
                    self.time(now),
                    row["job_id"],
                ),
            )
            claimed = connection.execute(
                "SELECT * FROM agent_jobs WHERE job_id = ?", (row["job_id"],)
            ).fetchone()
            connection.commit()
        return self._agent_job(claimed)

    def finish_agent_job(
        self,
        job_id: str,
        owner: str,
        state: AgentJobState,
        *,
        output: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> AgentJob:
        if state not in {AgentJobState.SUCCEEDED, AgentJobState.FAILED, AgentJobState.CANCELLED}:
            raise ValueError("finish state must be terminal")
        with self._lock, self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_jobs SET state = ?, output_json = ?, error = ?, lease_owner = NULL,
                    lease_until = NULL, updated_at = ?
                WHERE job_id = ? AND lease_owner = ? AND state = ?
                """,
                (
                    state.value,
                    _dump(output) if output is not None else None,
                    error[:1000] if error else None,
                    self.time(self.now()),
                    job_id,
                    owner,
                    AgentJobState.RUNNING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise Conflict("only the active worker lease can finish a running job")
            row = connection.execute(
                "SELECT * FROM agent_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return self._agent_job(row)

    def create_tool_call(self, call: ToolCall) -> ToolCall:
        from service_desk.tool_approval import digests_match, request_digest, write_target

        now = self.now()
        if not call.request_digest:
            raise Conflict("tool request is missing its integrity digest")
        computed_digest = request_digest(
            job_id=call.job_id,
            tool_name=call.tool_name,
            risk=call.risk,
            arguments=call.arguments,
            requested_by=call.requested_by,
        )
        if not digests_match(call.request_digest, computed_digest):
            raise Conflict("tool request digest does not match its reviewed fields")
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                "SELECT issue_id, state, lease_owner FROM agent_jobs WHERE job_id = ?",
                (call.job_id,),
            ).fetchone()
            if not job:
                raise NotFound(f"agent job {call.job_id!r} not found")
            if job["state"] != AgentJobState.RUNNING.value:
                raise Conflict("tool calls can be requested only by a running agent job")
            if job["lease_owner"] != call.requested_by:
                raise Conflict("tool caller must own the active agent-job lease")
            if call.risk is not ToolRisk.READ_ONLY:
                issue_id, expected_version = write_target(call.arguments)
                if issue_id != job["issue_id"]:
                    raise Conflict("write tool target must match the agent job issue")
                issue = connection.execute(
                    "SELECT version FROM issues WHERE issue_id = ?", (issue_id,)
                ).fetchone()
                if not issue:
                    raise NotFound(f"issue {issue_id!r} not found")
                if issue["version"] != expected_version:
                    raise Conflict("write tool expected_version is already stale")
            connection.execute(
                """
                INSERT INTO tool_calls(
                    call_id, job_id, tool_name, risk, arguments_json, state, requested_by,
                    approved_by, result_json, request_digest, approved_at,
                    approval_expires_at, execution_owner, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, NULL, NULL, NULL, ?, ?)
                """,
                (
                    call.call_id,
                    call.job_id,
                    call.tool_name,
                    call.risk.value,
                    _dump(call.arguments),
                    call.state,
                    call.requested_by,
                    call.request_digest,
                    self.time(now),
                    self.time(now),
                ),
            )
            if call.risk is not ToolRisk.READ_ONLY:
                connection.execute(
                    """
                    UPDATE agent_jobs SET state = ?, lease_owner = NULL, lease_until = NULL,
                        updated_at = ? WHERE job_id = ?
                    """,
                    (AgentJobState.WAITING_FOR_APPROVAL.value, self.time(now), call.job_id),
                )
            connection.commit()
        return call

    def approve_tool_call(
        self,
        call_id: str,
        approver_id: str,
        *,
        approval_ttl_seconds: int = 900,
    ) -> ToolCall:
        from service_desk.tool_approval import digests_match, request_digest

        if not 30 <= approval_ttl_seconds <= 3600:
            raise ValueError("approval_ttl_seconds must be in [30, 3600]")
        now = self.now()
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM tool_calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            if not row:
                raise NotFound(f"tool call {call_id!r} not found")
            if row["state"] != "waiting_for_approval":
                raise Conflict("tool call is not waiting for approval")
            if row["requested_by"] == approver_id:
                raise Conflict("high-impact tool calls require a different approver")
            if not row["request_digest"]:
                raise Conflict("legacy tool call has no review digest and must be recreated")
            computed_digest = request_digest(
                job_id=row["job_id"],
                tool_name=row["tool_name"],
                risk=ToolRisk(row["risk"]),
                arguments=json.loads(row["arguments_json"]),
                requested_by=row["requested_by"],
            )
            if not digests_match(row["request_digest"], computed_digest):
                raise Conflict("tool request changed after it was created")
            connection.execute(
                """
                UPDATE tool_calls SET state = 'approved', approved_by = ?, approved_at = ?,
                    approval_expires_at = ?, updated_at = ? WHERE call_id = ?
                """,
                (
                    approver_id,
                    self.time(now),
                    self.time(now + timedelta(seconds=approval_ttl_seconds)),
                    self.time(now),
                    call_id,
                ),
            )
            connection.execute(
                """
                UPDATE agent_jobs SET state = ?, updated_at = ?
                WHERE job_id = ? AND state = ?
                """,
                (
                    AgentJobState.QUEUED.value,
                    self.time(now),
                    row["job_id"],
                    AgentJobState.WAITING_FOR_APPROVAL.value,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM tool_calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            connection.commit()
        return self._tool_call(updated)

    def start_tool_call(
        self,
        call_id: str,
        execution_owner: str,
        expected_request_digest: str,
    ) -> ToolCall:
        """Atomically admit the exact reviewed call immediately before side effects."""
        from service_desk.tool_approval import digests_match, request_digest, write_target

        if not execution_owner.strip():
            raise ValueError("execution_owner must not be empty")
        now = self.now()
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM tool_calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            if not row:
                raise NotFound(f"tool call {call_id!r} not found")
            expected_state = "ready" if row["risk"] == ToolRisk.READ_ONLY.value else "approved"
            if row["state"] != expected_state:
                raise Conflict("tool call is not ready for execution admission")
            job = connection.execute(
                "SELECT state, lease_owner FROM agent_jobs WHERE job_id = ?", (row["job_id"],)
            ).fetchone()
            if (
                not job
                or job["state"] != AgentJobState.RUNNING.value
                or job["lease_owner"] != execution_owner
            ):
                raise Conflict("tool execution requires the active agent-job lease")
            stored_digest = row["request_digest"]
            if not stored_digest:
                raise Conflict("tool call has no review digest and must be recreated")
            computed_digest = request_digest(
                job_id=row["job_id"],
                tool_name=row["tool_name"],
                risk=ToolRisk(row["risk"]),
                arguments=json.loads(row["arguments_json"]),
                requested_by=row["requested_by"],
            )
            if not (
                digests_match(stored_digest, computed_digest)
                and digests_match(stored_digest, expected_request_digest)
            ):
                raise Conflict("tool request does not match the reviewed digest")
            if row["risk"] != ToolRisk.READ_ONLY.value:
                if not row["approval_expires_at"]:
                    raise Conflict("write tool approval has no expiry")
                if datetime.fromisoformat(row["approval_expires_at"]) <= now:
                    raise Conflict("write tool approval has expired")
                issue_id, expected_version = write_target(json.loads(row["arguments_json"]))
                issue = connection.execute(
                    "SELECT version FROM issues WHERE issue_id = ?", (issue_id,)
                ).fetchone()
                if not issue:
                    raise NotFound(f"issue {issue_id!r} not found")
                if issue["version"] != expected_version:
                    raise Conflict("write tool target changed after human approval")
            connection.execute(
                """
                UPDATE tool_calls SET state = 'executing', execution_owner = ?, updated_at = ?
                WHERE call_id = ?
                """,
                (execution_owner, self.time(now), call_id),
            )
            updated = connection.execute(
                "SELECT * FROM tool_calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            connection.commit()
        return self._tool_call(updated)

    def complete_tool_call(
        self, call_id: str, result: dict[str, Any], *, execution_owner: str
    ) -> ToolCall:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM tool_calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            if not row:
                raise NotFound(f"tool call {call_id!r} not found")
            if row["state"] != "executing" or row["execution_owner"] != execution_owner:
                raise Conflict("only the admitted execution owner can complete a tool call")
            connection.execute(
                "UPDATE tool_calls SET state = 'succeeded', result_json = ?, updated_at = ? WHERE call_id = ?",
                (_dump(result), self.time(self.now()), call_id),
            )
            updated = connection.execute(
                "SELECT * FROM tool_calls WHERE call_id = ?", (call_id,)
            ).fetchone()
        return self._tool_call(updated)

    def configure_sla_policy(self, policy: SLAPolicy) -> SLAPolicy:
        self.get_project(policy.project_id)
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO sla_policies(
                    project_id, priority, first_response_seconds, resolution_seconds
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(project_id, priority) DO UPDATE SET
                    first_response_seconds = excluded.first_response_seconds,
                    resolution_seconds = excluded.resolution_seconds
                """,
                (
                    policy.project_id,
                    policy.priority.value,
                    policy.first_response_seconds,
                    policy.resolution_seconds,
                ),
            )
        return policy

    def get_sla_state(self, issue_id: str) -> SLAState:
        now = self.now()
        with self.connect() as connection:
            self._get_issue(connection, issue_id)
            row = connection.execute(
                "SELECT * FROM sla_instances WHERE issue_id = ?", (issue_id,)
            ).fetchone()
        if not row:
            raise NotFound(f"issue {issue_id!r} has no matching SLA policy")
        first_due = datetime.fromisoformat(row["first_response_due_at"])
        resolution_due = datetime.fromisoformat(row["resolution_due_at"])
        paused_at = datetime.fromisoformat(row["paused_at"]) if row["paused_at"] else None
        first_response = (
            datetime.fromisoformat(row["first_responded_at"]) if row["first_responded_at"] else None
        )
        resolved = datetime.fromisoformat(row["resolved_at"]) if row["resolved_at"] else None
        reference = paused_at or now
        first_observed = first_response or reference
        resolution_observed = resolved or reference
        return SLAState(
            issue_id=issue_id,
            first_response_due_at=first_due,
            resolution_due_at=resolution_due,
            paused_at=paused_at,
            total_paused_seconds=int(row["total_paused_seconds"]),
            first_responded_at=first_response,
            resolved_at=resolved,
            first_response_breached=first_observed > first_due,
            resolution_breached=resolution_observed > resolution_due,
            first_response_remaining_seconds=max(
                0, int((first_due - first_observed).total_seconds())
            ),
            resolution_remaining_seconds=max(
                0, int((resolution_due - resolution_observed).total_seconds())
            ),
        )

    def create_issue_link(self, link: IssueLink) -> IssueLink:
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            source = self._get_issue(connection, link.source_issue_id)
            target = self._get_issue(connection, link.target_issue_id)
            if source.project_id != target.project_id and link.link_type is IssueLinkType.PARENT_OF:
                raise Conflict("parent and child issues must belong to the same project")
            if link.source_issue_id == link.target_issue_id:
                raise Conflict("an issue cannot link to itself")
            if link.link_type is IssueLinkType.PARENT_OF:
                cycle = connection.execute(
                    """
                    WITH RECURSIVE descendants(issue_id) AS (
                        SELECT target_issue_id FROM issue_links
                        WHERE source_issue_id = ? AND link_type = 'parent_of'
                        UNION ALL
                        SELECT links.target_issue_id FROM issue_links AS links
                        JOIN descendants ON links.source_issue_id = descendants.issue_id
                        WHERE links.link_type = 'parent_of'
                    )
                    SELECT 1 FROM descendants WHERE issue_id = ? LIMIT 1
                    """,
                    (link.target_issue_id, link.source_issue_id),
                ).fetchone()
                if cycle:
                    raise Conflict("parent link would create an issue hierarchy cycle")
            try:
                connection.execute(
                    """
                    INSERT INTO issue_links(
                        link_id, source_issue_id, target_issue_id, link_type, created_by, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        link.link_id,
                        link.source_issue_id,
                        link.target_issue_id,
                        link.link_type.value,
                        link.created_by,
                        self.time(link.created_at),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("issue link already exists or child already has a parent") from exc
            self._audit(
                connection,
                link.link_id + "-audit",
                link.target_issue_id,
                "issue_link_created",
                link.created_by,
                {
                    "source_issue_id": link.source_issue_id,
                    "link_type": link.link_type.value,
                },
            )
            self._outbox(
                connection,
                link.link_id + "-outbox",
                link.target_issue_id,
                "issue.link_created",
                {
                    "source_issue_id": link.source_issue_id,
                    "target_issue_id": link.target_issue_id,
                    "link_type": link.link_type.value,
                },
            )
            connection.commit()
        return link

    def list_issue_links(self, issue_id: str) -> list[IssueLink]:
        with self.connect() as connection:
            self._get_issue(connection, issue_id)
            rows = connection.execute(
                """
                SELECT * FROM issue_links
                WHERE source_issue_id = ? OR target_issue_id = ?
                ORDER BY created_at, link_id
                """,
                (issue_id, issue_id),
            ).fetchall()
        return [
            IssueLink(
                link_id=row["link_id"],
                source_issue_id=row["source_issue_id"],
                target_issue_id=row["target_issue_id"],
                link_type=IssueLinkType(row["link_type"]),
                created_by=row["created_by"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def issue_descendants(self, issue_id: str) -> list[Issue]:
        with self.connect() as connection:
            self._get_issue(connection, issue_id)
            rows = connection.execute(
                """
                WITH RECURSIVE descendants(issue_id, depth) AS (
                    SELECT target_issue_id, 1 FROM issue_links
                    WHERE source_issue_id = ? AND link_type = 'parent_of'
                    UNION ALL
                    SELECT links.target_issue_id, descendants.depth + 1
                    FROM issue_links AS links
                    JOIN descendants ON links.source_issue_id = descendants.issue_id
                    WHERE links.link_type = 'parent_of'
                )
                SELECT issues.* FROM descendants
                JOIN issues ON issues.issue_id = descendants.issue_id
                ORDER BY descendants.depth, issues.issue_sequence
                """,
                (issue_id,),
            ).fetchall()
        return [self._issue(row) for row in rows]

    def audit_stream(self, issue_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_events WHERE issue_id = ? ORDER BY occurred_at, event_id",
                (issue_id,),
            ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "occurred_at": row["occurred_at"],
            }
            for row in rows
        ]

    def _update_sla_for_transition(
        self,
        connection: sqlite3.Connection,
        source: IssueStatus,
        target: IssueStatus,
        issue_id: str,
        now: datetime,
    ) -> None:
        row = connection.execute(
            "SELECT * FROM sla_instances WHERE issue_id = ?", (issue_id,)
        ).fetchone()
        if not row:
            return
        if target is IssueStatus.WAITING_FOR_CUSTOMER and row["paused_at"] is None:
            connection.execute(
                "UPDATE sla_instances SET paused_at = ?, updated_at = ? WHERE issue_id = ?",
                (self.time(now), self.time(now), issue_id),
            )
            return
        if source is IssueStatus.WAITING_FOR_CUSTOMER and row["paused_at"]:
            paused_at = datetime.fromisoformat(row["paused_at"])
            pause_seconds = max(0, int((now - paused_at).total_seconds()))
            first_due = datetime.fromisoformat(row["first_response_due_at"]) + timedelta(
                seconds=pause_seconds
            )
            resolution_due = datetime.fromisoformat(row["resolution_due_at"]) + timedelta(
                seconds=pause_seconds
            )
            connection.execute(
                """
                UPDATE sla_instances SET first_response_due_at = ?, resolution_due_at = ?,
                    paused_at = NULL, total_paused_seconds = total_paused_seconds + ?,
                    updated_at = ? WHERE issue_id = ?
                """,
                (
                    self.time(first_due),
                    self.time(resolution_due),
                    pause_seconds,
                    self.time(now),
                    issue_id,
                ),
            )
        if target in {IssueStatus.RESOLVED, IssueStatus.CLOSED}:
            connection.execute(
                """
                UPDATE sla_instances SET resolved_at = COALESCE(resolved_at, ?),
                    updated_at = ? WHERE issue_id = ?
                """,
                (self.time(now), self.time(now), issue_id),
            )

    def _get_issue(self, connection: sqlite3.Connection, issue_id: str) -> Issue:
        row = connection.execute("SELECT * FROM issues WHERE issue_id = ?", (issue_id,)).fetchone()
        if not row:
            raise NotFound(f"issue {issue_id!r} not found")
        return self._issue(row)

    @staticmethod
    def _issue(row: sqlite3.Row) -> Issue:
        return Issue(
            issue_id=row["issue_id"],
            issue_key=row["issue_key"],
            project_id=row["project_id"],
            sequence=row["issue_sequence"],
            summary=row["summary"],
            description=row["description"],
            status=IssueStatus(row["status"]),
            priority=IssuePriority(row["priority"]),
            reporter_id=row["reporter_id"],
            assignee_id=row["assignee_id"],
            resolution=row["resolution"],
            version=row["version"],
            labels=tuple(json.loads(row["labels_json"])),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _agent_job(row: sqlite3.Row) -> AgentJob:
        return AgentJob(
            job_id=row["job_id"],
            issue_id=row["issue_id"],
            capability=row["capability"],
            state=AgentJobState(row["state"]),
            input_payload=json.loads(row["input_json"]),
            output_payload=json.loads(row["output_json"]) if row["output_json"] else None,
            error=row["error"],
            attempt=row["attempt"],
            max_attempts=row["max_attempts"],
            lease_owner=row["lease_owner"],
            lease_until=datetime.fromisoformat(row["lease_until"]) if row["lease_until"] else None,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _tool_call(row: sqlite3.Row) -> ToolCall:
        return ToolCall(
            call_id=row["call_id"],
            job_id=row["job_id"],
            tool_name=row["tool_name"],
            risk=ToolRisk(row["risk"]),
            arguments=json.loads(row["arguments_json"]),
            state=row["state"],
            requested_by=row["requested_by"],
            approved_by=row["approved_by"],
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            request_digest=row["request_digest"],
            approved_at=datetime.fromisoformat(row["approved_at"]) if row["approved_at"] else None,
            approval_expires_at=datetime.fromisoformat(row["approval_expires_at"])
            if row["approval_expires_at"]
            else None,
            execution_owner=row["execution_owner"],
        )

    def _audit(
        self,
        connection: sqlite3.Connection,
        event_id: str,
        issue_id: str,
        event_type: str,
        actor_id: str,
        payload: dict[str, Any],
    ) -> None:
        connection.execute(
            "INSERT INTO audit_events VALUES (?, ?, ?, ?, ?, ?)",
            (event_id, issue_id, event_type, actor_id, _dump(payload), self.time(self.now())),
        )

    def _outbox(
        self,
        connection: sqlite3.Connection,
        event_id: str,
        aggregate_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        now = self.now()
        connection.execute(
            """
            INSERT INTO outbox_events(
                event_id, aggregate_id, event_type, payload_json, available_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (event_id, aggregate_id, event_type, _dump(payload), self.time(now), self.time(now)),
        )
