from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
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
    # Explicit built-in analytics functions only. No extensions or application UDFs
    # are registered on this tool's private, per-execution connection.
    _max_sql_bytes = 64 * 1024
    _max_pattern_bytes = 1024
    _allowed_functions = frozenset(
        {
            "abs",
            "avg",
            "coalesce",
            "count",
            "ifnull",
            "length",
            "lower",
            "ltrim",
            "max",
            "min",
            "nullif",
            "printf",
            "round",
            "rtrim",
            "substr",
            "substring",
            "sum",
            "total",
            "trim",
            "typeof",
            "upper",
            "like",
            "glob",
        }
    )

    def __init__(
        self,
        database_path: str,
        *,
        allowed_tables: frozenset[str] = frozenset({"issues", "projects"}),
        max_rows: int = 100,
        max_vm_steps: int = 100_000,
        max_result_bytes: int = 64 * 1024,
        max_execution_seconds: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        for name, value in (
            ("max_rows", max_rows),
            ("max_vm_steps", max_vm_steps),
            ("max_result_bytes", max_result_bytes),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if max_result_bytes > 16 * 1024 * 1024:
            raise ValueError("max_result_bytes must not exceed 16777216")
        if (
            isinstance(max_execution_seconds, bool)
            or not isinstance(max_execution_seconds, (int, float))
            or not math.isfinite(max_execution_seconds)
            or not 0 < max_execution_seconds <= 300
        ):
            raise ValueError("max_execution_seconds must be a finite number between 0 and 300")
        self.database_path = database_path
        self.allowed_tables = frozenset(name.lower() for name in allowed_tables)
        self.max_rows = max_rows
        self.max_vm_steps = max_vm_steps
        self.max_result_bytes = max_result_bytes
        self.max_execution_seconds = float(max_execution_seconds)
        self._clock = clock

    def validate(self, sql: str) -> str:
        # Shape validation only, never an authorization decision. SQLite itself
        # parses quoted identifiers, comments, CTEs and statement boundaries.
        if len(sql.encode("utf-8")) > self._max_sql_bytes:
            raise ToolPolicyViolation("query exceeded SQL text byte budget")
        candidate = sql.strip()
        if not candidate:
            raise ToolPolicyViolation("SQL tool requires a nonempty query")
        return candidate

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        sql = self.validate(str(arguments.get("sql", "")))
        parameters = arguments.get("parameters", {})
        if not isinstance(parameters, dict):
            raise ToolPolicyViolation("parameters must be a JSON object")
        query_fingerprint = self._query_fingerprint(sql, parameters)
        deadline = self._clock() + self.max_execution_seconds
        # The server owns these budgets; model-supplied arguments cannot raise them.
        remaining_steps = self.max_vm_steps
        progress_interval = min(1000, remaining_steps)
        vm_exhausted = False
        deadline_exhausted = False

        def check_budget() -> int:
            nonlocal remaining_steps, vm_exhausted, deadline_exhausted
            if self._clock() >= deadline:
                deadline_exhausted = True
                return 1
            remaining_steps -= progress_interval
            vm_exhausted = remaining_steps <= 0
            return int(vm_exhausted)

        def require_time() -> None:
            nonlocal deadline_exhausted
            if self._clock() >= deadline:
                deadline_exhausted = True
                raise ToolPolicyViolation("query exceeded wall-clock budget")

        read_allowed_table = False
        authorization_denied = False

        def authorize(action, first, second, database, source):
            nonlocal read_allowed_table, authorization_denied
            if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_READ:
                # SQLite reports database=None for a table read with no columns
                # (e.g. count(*)). This private connection has only main and an
                # empty temp schema; ATTACH and temp object creation are denied.
                if (database == "main" or (database is None and second == "")) and (
                    first and first.lower() in readable_tables
                ):
                    read_allowed_table = True
                    return sqlite3.SQLITE_OK
            elif (
                action == sqlite3.SQLITE_FUNCTION
                and second
                and second.lower() in self._allowed_functions
            ):
                return sqlite3.SQLITE_OK
            authorization_denied = True
            return sqlite3.SQLITE_DENY

        uri = f"file:{self.database_path}?mode=ro"
        try:
            require_time()
            with closing(
                sqlite3.connect(
                    uri,
                    uri=True,
                    timeout=min(2.0, self.max_execution_seconds),
                )
            ) as connection:
                connection.row_factory = sqlite3.Row
                remaining_ms = max(1, int((deadline - self._clock()) * 1000))
                connection.execute(f"PRAGMA busy_timeout = {min(2000, remaining_ms)}")
                connection.execute("PRAGMA query_only = ON")
                connection.execute("PRAGMA trusted_schema = OFF")
                # Only ordinary main-schema tables, not views/virtual tables or
                # shadow tables. Database schema and tool configuration are trusted.
                readable_tables = {
                    row[1].lower()
                    for row in connection.execute("PRAGMA main.table_list")
                    if row[0] == "main"
                    and row[2] == "table"
                    and not row[1].lower().startswith("sqlite_")
                    and row[1].lower() in self.allowed_tables
                }
                # Native value/record bounds constrain individual built-in results
                # before Python fetching/JSON checks. Not a process memory limit.
                connection.setlimit(
                    sqlite3.SQLITE_LIMIT_LENGTH, max(64 * 1024, self.max_result_bytes)
                )
                connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, self._max_sql_bytes)
                connection.setlimit(
                    sqlite3.SQLITE_LIMIT_LIKE_PATTERN_LENGTH, self._max_pattern_bytes
                )
                connection.set_authorizer(authorize)
                connection.set_progress_handler(check_budget, progress_interval)
                require_time()
                cursor = connection.execute(sql, parameters)
                if not read_allowed_table or cursor.description is None:
                    raise ToolPolicyViolation("query must reference an allowlisted table")
                columns = [column[0] for column in cursor.description or ()]
                rows: list[dict[str, Any]] = []
                truncated = False
                for row in cursor:
                    require_time()
                    if len(rows) == self.max_rows:
                        truncated = True
                        break
                    rows.append(dict(row))
                    self._require_result_budget(
                        self._result_payload(
                            columns,
                            rows,
                            truncated=False,
                            job_id=context.job_id,
                            query_fingerprint=query_fingerprint,
                        )
                    )
                require_time()
                result = self._result_payload(
                    columns,
                    rows,
                    truncated=truncated,
                    job_id=context.job_id,
                    query_fingerprint=query_fingerprint,
                )
                self._require_result_budget(result)
                return result
        except ToolPolicyViolation:
            raise
        except sqlite3.Error as exc:
            if authorization_denied:
                raise ToolPolicyViolation(
                    "query contains a forbidden operation or non-allowlisted access"
                ) from None
            if isinstance(exc, sqlite3.ProgrammingError) and "one statement" in str(exc):
                raise ToolPolicyViolation("multiple SQL statements are forbidden") from None
            if deadline_exhausted or self._clock() >= deadline:
                raise ToolPolicyViolation("query exceeded wall-clock budget") from None
            if "locked" in str(exc).lower():
                # Lock acquisition consumed the configured busy timeout. Keep the
                # public failure stable without exposing database details.
                raise ToolPolicyViolation("query exceeded wall-clock budget") from None
            if vm_exhausted:
                raise ToolPolicyViolation("query exceeded VM instruction budget") from None
            if isinstance(exc, sqlite3.DataError):
                raise ToolPolicyViolation("query result exceeded byte budget") from None
            raise ToolPolicyViolation("query rejected by database") from None

    @staticmethod
    def _query_fingerprint(sql: str, parameters: dict[str, Any]) -> str:
        evidence = json.dumps(
            {
                "sql": sql,
                "parameter_names": sorted(str(name) for name in parameters),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(evidence).hexdigest()

    @staticmethod
    def _result_payload(
        columns: list[str],
        rows: list[dict[str, Any]],
        *,
        truncated: bool,
        job_id: str,
        query_fingerprint: str,
    ) -> dict[str, Any]:
        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
            "job_id": job_id,
            "query_fingerprint": query_fingerprint,
        }

    def _require_result_budget(self, payload: dict[str, Any]) -> None:
        try:
            rendered = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ToolPolicyViolation("query result is not bounded JSON") from exc
        if len(rendered) > self.max_result_bytes:
            raise ToolPolicyViolation("query result exceeded byte budget")


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
