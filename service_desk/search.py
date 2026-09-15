"""A deliberately small, parameterized JQL-like issue query compiler."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from typing import Any

from service_desk.errors import InvalidIssueQuery
from service_desk.models import IssuePriority, IssueStatus


@dataclass(frozen=True, slots=True)
class CompiledIssueQuery:
    where_sql: str
    parameters: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class IssueCursor:
    updated_at: str
    sequence: int

    def encode(self) -> str:
        raw = json.dumps([self.updated_at, self.sequence], separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @classmethod
    def decode(cls, value: str) -> IssueCursor:
        try:
            padded = value + "=" * (-len(value) % 4)
            decoded = json.loads(base64.urlsafe_b64decode(padded))
            if (
                not isinstance(decoded, list)
                or len(decoded) != 2
                or not isinstance(decoded[0], str)
                or not isinstance(decoded[1], int)
                or decoded[1] < 1
            ):
                raise ValueError
            return cls(decoded[0], decoded[1])
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise InvalidIssueQuery("invalid pagination cursor") from exc


_CLAUSE = re.compile(
    r"(?P<field>status|priority|assignee|reporter|label|text|sla)\s*"
    r"(?P<operator>=|!=|~|IN)\s*"
    r"(?P<value>\([^)]*\)|\"[^\"]*\"|'[^']*'|[A-Za-z0-9_.@:-]+)",
    re.IGNORECASE,
)


def compile_issue_query(expression: str | None) -> CompiledIssueQuery:
    if expression is None or not expression.strip():
        return CompiledIssueQuery("", ())
    if len(expression) > 1000:
        raise InvalidIssueQuery("query is too long")
    clauses = re.split(r"\s+AND\s+", expression.strip(), flags=re.IGNORECASE)
    if len(clauses) > 10:
        raise InvalidIssueQuery("query has too many clauses")
    sql: list[str] = []
    parameters: list[Any] = []
    for clause in clauses:
        match = _CLAUSE.fullmatch(clause.strip())
        if not match:
            raise InvalidIssueQuery(f"unsupported query clause: {clause!r}")
        field = match["field"].lower()
        operator = match["operator"].upper()
        values = _values(match["value"])
        if operator == "IN":
            if field not in {"status", "priority"} or not 1 <= len(values) <= 10:
                raise InvalidIssueQuery("IN supports 1-10 status or priority values")
            normalized = [_validate_enum(field, value) for value in values]
            placeholders = ",".join("?" for _ in normalized)
            sql.append(f"issues.{field} IN ({placeholders})")
            parameters.extend(normalized)
            continue
        if len(values) != 1:
            raise InvalidIssueQuery("scalar operator requires one value")
        value = values[0]
        if field in {"status", "priority"}:
            value = _validate_enum(field, value)
        if field == "text":
            if operator != "~":
                raise InvalidIssueQuery("text supports only the ~ operator")
            escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            sql.append(
                "(issues.summary LIKE ? ESCAPE '\\' OR issues.description LIKE ? ESCAPE '\\')"
            )
            parameters.extend((f"%{escaped}%", f"%{escaped}%"))
        elif field == "label":
            if operator not in {"=", "!="}:
                raise InvalidIssueQuery("label supports only = and !=")
            exists = "EXISTS" if operator == "=" else "NOT EXISTS"
            sql.append(f"{exists} (SELECT 1 FROM json_each(issues.labels_json) WHERE value = ?)")
            parameters.append(value.lower())
        elif field == "sla":
            if operator != "=" or value not in {"breached", "at_risk", "on_track", "none"}:
                raise InvalidIssueQuery("sla must be breached, at_risk, on_track or none")
            predicate, args = _sla_predicate(value)
            sql.append(predicate)
            parameters.extend(args)
        else:
            if operator not in {"=", "!="}:
                raise InvalidIssueQuery(f"{field} supports only = and !=")
            column = {"assignee": "assignee_id", "reporter": "reporter_id"}.get(field, field)
            sql.append(f"issues.{column} {operator} ?")
            parameters.append(value)
    return CompiledIssueQuery(" AND ".join(sql), tuple(parameters))


def _values(raw: str) -> list[str]:
    if raw.startswith("("):
        values = [value.strip().strip("\"'") for value in raw[1:-1].split(",")]
    else:
        values = [raw.strip("\"'")]
    if any(not value or len(value) > 240 for value in values):
        raise InvalidIssueQuery("query values must contain 1-240 characters")
    return values


def _validate_enum(field: str, value: str) -> str:
    allowed = IssueStatus if field == "status" else IssuePriority
    try:
        return allowed(value.lower()).value
    except ValueError as exc:
        raise InvalidIssueQuery(f"unknown {field} value {value!r}") from exc


def _sla_predicate(value: str) -> tuple[str, tuple[Any, ...]]:
    if value == "none":
        return "sla.issue_id IS NULL", ()
    if value == "breached":
        return (
            (
                "sla.issue_id IS NOT NULL AND sla.resolved_at IS NULL "
                "AND sla.paused_at IS NULL AND sla.resolution_due_at < ?"
            ),
            ("__NOW__",),
        )
    if value == "at_risk":
        return (
            (
                "sla.issue_id IS NOT NULL AND sla.resolved_at IS NULL "
                "AND sla.paused_at IS NULL AND sla.resolution_due_at >= ? "
                "AND sla.resolution_due_at <= ?"
            ),
            ("__NOW__", "__RISK_HORIZON__"),
        )
    return (
        (
            "sla.issue_id IS NOT NULL AND sla.resolved_at IS NULL "
            "AND sla.paused_at IS NULL AND sla.resolution_due_at > ?"
        ),
        ("__RISK_HORIZON__",),
    )
