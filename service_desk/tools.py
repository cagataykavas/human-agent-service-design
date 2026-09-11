from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Protocol

from service_desk.errors import ToolPolicyViolation
from service_desk.models import ToolRisk


@dataclass(frozen=True, slots=True)
class ToolContext:
    actor_id: str
    job_id: str


class Tool(Protocol):
    name: str
    risk: ToolRisk

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]: ...


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} is already registered")
        self._tools[tool.name] = tool

    def resolve(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolPolicyViolation(f"tool {name!r} is not allowlisted") from exc

    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        return self.resolve(name).execute(arguments, context)


class ReadOnlySQLTool:
    """Bounded SQL tool for agent analytics against an allowlisted read model."""

    name = "service_desk_sql"
    risk = ToolRisk.READ_ONLY
    _forbidden = re.compile(
        r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|ATTACH|DETACH|PRAGMA|VACUUM|REINDEX|TRIGGER)\b",
        re.IGNORECASE,
    )
    _table_reference = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)

    def __init__(
        self,
        database_path: str,
        *,
        allowed_tables: frozenset[str] = frozenset({"issues", "projects"}),
        max_rows: int = 100,
    ) -> None:
        self.database_path = database_path
        self.allowed_tables = allowed_tables
        self.max_rows = max_rows

    def validate(self, sql: str) -> str:
        candidate = sql.strip()
        if self._forbidden.search(candidate):
            raise ToolPolicyViolation("query contains a forbidden SQL operation")
        if not candidate or not re.match(r"^(SELECT|WITH)\b", candidate, re.IGNORECASE):
            raise ToolPolicyViolation("SQL tool accepts only SELECT or WITH queries")
        without_final = candidate.removesuffix(";")
        if ";" in without_final:
            raise ToolPolicyViolation("multiple SQL statements are forbidden")
        referenced = {match.lower() for match in self._table_reference.findall(candidate)}
        forbidden_tables = referenced - self.allowed_tables
        if forbidden_tables:
            raise ToolPolicyViolation(
                "query references non-allowlisted tables: " + ", ".join(sorted(forbidden_tables))
            )
        if not referenced:
            raise ToolPolicyViolation("query must reference an allowlisted table")
        return without_final

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        sql = self.validate(str(arguments.get("sql", "")))
        parameters = arguments.get("parameters", {})
        if not isinstance(parameters, dict):
            raise ToolPolicyViolation("parameters must be a JSON object")
        uri = f"file:{self.database_path}?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True, timeout=2) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only = ON")
                connection.execute("PRAGMA busy_timeout = 2000")
                cursor = connection.execute(sql, parameters)
                columns = [column[0] for column in cursor.description or ()]
                rows = cursor.fetchmany(self.max_rows + 1)
        except sqlite3.Error as exc:
            raise ToolPolicyViolation(f"query rejected by database: {exc}") from exc
        truncated = len(rows) > self.max_rows
        selected = rows[: self.max_rows]
        return {
            "columns": columns,
            "rows": [dict(row) for row in selected],
            "row_count": len(selected),
            "truncated": truncated,
            "job_id": context.job_id,
        }


class KnowledgeSearchTool:
    """Deterministic local retrieval adapter standing in for a vector index."""

    name = "knowledge_search"
    risk = ToolRisk.READ_ONLY

    def __init__(self, documents: dict[str, str], max_results: int = 5) -> None:
        self.documents = dict(documents)
        self.max_results = max_results

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        query = str(arguments.get("query", "")).strip().lower()
        if not query:
            raise ToolPolicyViolation("knowledge query must not be empty")
        terms = {term for term in re.findall(r"[a-z0-9_]+", query) if len(term) > 2}
        scored = []
        for document_id, text in self.documents.items():
            normalized = text.lower()
            score = sum(term in normalized for term in terms)
            if score:
                scored.append((score, document_id, text))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return {
            "query": query,
            "results": [
                {"document_id": document_id, "score": score, "text": text}
                for score, document_id, text in scored[: self.max_results]
            ],
            "job_id": context.job_id,
        }
