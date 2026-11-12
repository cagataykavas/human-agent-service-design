from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

EVIDENCE_SCHEMA = "agent-run-audit/v1"
MAX_JOBS = 10_000
MAX_TOOL_CALLS = 50_000
MAX_JSON_BYTES = 65_536
MAX_JSON_DEPTH = 16
MAX_JSON_NODES = 4_096
MAX_TEXT_BYTES = 16_384
CAPABILITIES = {"triage", "summarize", "retrieve_knowledge", "recommend_action"}
STATES = {"queued", "running", "waiting_for_approval", "succeeded", "failed", "cancelled"}
TERMINAL_STATES = {"succeeded", "failed", "cancelled"}


class AuditInputError(ValueError):
    """Raised when the database cannot be audited safely."""


class ContractError(ValueError):
    """Raised when one persisted value violates the run contract."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _reject_constant(_value: str) -> None:
    raise ContractError("non-finite JSON numbers are forbidden")


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate JSON field {key!r}")
        result[key] = value
    return result


def _load_json(raw: Any, label: str) -> Any:
    if not isinstance(raw, str):
        raise ContractError(f"{label} must be stored as text")
    if len(raw.encode("utf-8")) > MAX_JSON_BYTES:
        raise ContractError(f"{label} exceeds {MAX_JSON_BYTES} bytes")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_pairs_without_duplicates,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, UnicodeError, RecursionError) as exc:
        raise ContractError(f"{label} is not strict JSON") from exc
    _validate_json_tree(value, label)
    return value


def _validate_json_tree(value: Any, label: str) -> None:
    stack = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise ContractError(f"{label} exceeds {MAX_JSON_NODES} JSON nodes")
        if depth > MAX_JSON_DEPTH:
            raise ContractError(f"{label} exceeds JSON depth {MAX_JSON_DEPTH}")
        if current is None or isinstance(current, bool):
            continue
        if isinstance(current, int):
            if not -(2**63) <= current <= 2**63 - 1:
                raise ContractError(f"{label} integer is outside signed 64-bit range")
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                raise ContractError(f"{label} contains a non-finite number")
            continue
        if isinstance(current, str):
            if len(current.encode("utf-8")) > MAX_TEXT_BYTES:
                raise ContractError(f"{label} contains text exceeding {MAX_TEXT_BYTES} bytes")
            continue
        if isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
            continue
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
            continue
        raise ContractError(f"{label} contains an unsupported JSON value")


def _exact_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be an object")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if unknown:
            details.append("unknown=" + ",".join(unknown))
        raise ContractError(f"{label} fields do not match contract ({'; '.join(details)})")
    return value


def _bounded_text(value: Any, label: str, *, minimum: int = 1, maximum: int = 240) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{label} must be a string")
    length = len(value.encode("utf-8"))
    if not minimum <= length <= maximum:
        raise ContractError(f"{label} must be between {minimum} and {maximum} UTF-8 bytes")
    return value


def _confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError("confidence must be numeric")
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise ContractError("confidence must be finite and in [0, 1]")
    return result


def _validate_output(capability: str, output: Any) -> dict[str, Any]:
    if capability == "triage":
        result = _exact_keys(
            output, {"category", "priority", "confidence", "reason_codes"}, "triage output"
        )
        if result["category"] not in {"security", "incident", "service_request"}:
            raise ContractError("triage category is not allowlisted")
        if result["priority"] not in {"low", "medium", "high", "critical"}:
            raise ContractError("triage priority is invalid")
        _confidence(result["confidence"])
        reasons = result["reason_codes"]
        if not isinstance(reasons, list) or not 1 <= len(reasons) <= 8:
            raise ContractError("triage reason_codes must contain 1 to 8 values")
        normalized = [_bounded_text(item, "reason code", maximum=64) for item in reasons]
        if len(normalized) != len(set(normalized)):
            raise ContractError("triage reason_codes must be unique")
        return result
    if capability == "summarize":
        result = _exact_keys(output, {"summary", "confidence"}, "summary output")
        _bounded_text(result["summary"], "summary", minimum=0, maximum=240)
        _confidence(result["confidence"])
        return result
    if capability == "recommend_action":
        result = _exact_keys(
            output,
            {"recommendation", "confidence", "requires_human"},
            "recommendation output",
        )
        _bounded_text(result["recommendation"], "recommendation", maximum=100)
        _confidence(result["confidence"])
        if not isinstance(result["requires_human"], bool):
            raise ContractError("requires_human must be a boolean")
        return result
    if capability == "retrieve_knowledge":
        result = _exact_keys(output, {"query", "confidence", "evidence"}, "retrieval output")
        _bounded_text(result["query"], "retrieval query", maximum=200)
        _confidence(result["confidence"])
        evidence = result["evidence"]
        if not isinstance(evidence, list) or len(evidence) > 5:
            raise ContractError("retrieval evidence must contain at most 5 items")
        document_ids: list[str] = []
        for index, item in enumerate(evidence):
            item = _exact_keys(item, {"document_id", "score", "text"}, f"evidence item {index}")
            document_ids.append(
                _bounded_text(
                    item["document_id"], f"evidence item {index} document_id", maximum=200
                )
            )
            if isinstance(item["score"], bool) or not isinstance(item["score"], int):
                raise ContractError(f"evidence item {index} score must be an integer")
            if not 1 <= item["score"] <= 1_000:
                raise ContractError(f"evidence item {index} score is out of range")
            _bounded_text(item["text"], f"evidence item {index} text", maximum=MAX_TEXT_BYTES)
        if len(document_ids) != len(set(document_ids)):
            raise ContractError("retrieval evidence document IDs must be unique")
        return result
    raise ContractError(f"unsupported capability {capability!r}")


def _parse_time(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise ContractError(f"{label} must be text")
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ContractError(f"{label} is not ISO-8601") from exc
    if timestamp.tzinfo is None:
        raise ContractError(f"{label} must include a timezone")
    return timestamp


def _validate_schema(connection: sqlite3.Connection) -> None:
    required = {
        "agent_jobs": {
            "job_id",
            "capability",
            "state",
            "input_json",
            "output_json",
            "error",
            "attempt",
            "max_attempts",
            "lease_owner",
            "lease_until",
            "created_at",
            "updated_at",
        },
        "tool_calls": {
            "call_id",
            "job_id",
            "tool_name",
            "risk",
            "arguments_json",
            "state",
            "result_json",
            "requested_by",
            "approved_by",
            "created_at",
            "updated_at",
        },
    }
    for table, expected in required.items():
        columns = {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})")}
        missing = sorted(expected - columns)
        if missing:
            raise AuditInputError(f"{table} is missing required columns: {', '.join(missing)}")


def _row_commitment(job: sqlite3.Row, calls: list[sqlite3.Row]) -> str:
    payload = {
        "job": {key: job[key] for key in tuple(job.keys())},
        "tool_calls": [{key: call[key] for key in tuple(call.keys())} for call in calls],
    }
    return _sha256(_canonical_json(payload))


def _finding(code: str, job_id: str, detail: str | None = None) -> dict[str, str]:
    finding = {"code": code, "job_ref": _sha256(job_id.encode("utf-8"))[:24]}
    if detail:
        finding["detail"] = detail
    return finding


def _audit_job(job: sqlite3.Row, calls: list[sqlite3.Row]) -> tuple[list[dict[str, str]], bool]:
    job_id = str(job["job_id"])
    findings: list[dict[str, str]] = []
    capability = job["capability"]
    state = job["state"]
    if capability not in CAPABILITIES:
        findings.append(_finding("unsupported_capability", job_id))
    if state not in STATES:
        findings.append(_finding("invalid_job_state", job_id))
        return findings, False

    try:
        input_payload = _load_json(job["input_json"], "input_json")
        if not isinstance(input_payload, dict):
            raise ContractError("input_json must be an object")
    except ContractError as exc:
        findings.append(_finding("invalid_input_json", job_id, str(exc)))

    try:
        attempt = int(job["attempt"])
        maximum = int(job["max_attempts"])
        if isinstance(job["attempt"], bool) or isinstance(job["max_attempts"], bool):
            raise TypeError
        if not 1 <= maximum <= 10 or not 0 <= attempt <= maximum:
            raise ValueError
        created = _parse_time(job["created_at"], "created_at")
        updated = _parse_time(job["updated_at"], "updated_at")
        if updated < created:
            raise ContractError("updated_at precedes created_at")
    except (TypeError, ValueError, ContractError) as exc:
        findings.append(_finding("invalid_job_metadata", job_id, str(exc)))
        attempt = -1

    output: dict[str, Any] | None = None
    if state in TERMINAL_STATES and (
        job["lease_owner"] is not None or job["lease_until"] is not None
    ):
        findings.append(_finding("terminal_lease_present", job_id))
    if state == "running":
        if not job["lease_owner"] or not job["lease_until"] or attempt < 1:
            findings.append(_finding("running_lease_missing", job_id))
    elif state in {"queued", "waiting_for_approval"} and (
        job["lease_owner"] is not None or job["lease_until"] is not None
    ):
        findings.append(_finding("inactive_lease_present", job_id))
    if state not in TERMINAL_STATES and (
        job["output_json"] is not None or job["error"] is not None
    ):
        findings.append(_finding("nonterminal_outcome_present", job_id))

    if state == "succeeded":
        if job["error"] is not None:
            findings.append(_finding("successful_job_has_error", job_id))
        try:
            output = _load_json(job["output_json"], "output_json")
            output = _validate_output(str(capability), output)
        except ContractError as exc:
            findings.append(_finding("invalid_output_contract", job_id, str(exc)))
            output = None
    elif state in {"failed", "cancelled"}:
        if job["output_json"] is not None:
            findings.append(_finding("unsuccessful_job_has_output", job_id))
        error = job["error"]
        if state == "failed" and (not isinstance(error, str) or not error.strip()):
            findings.append(_finding("failed_job_missing_error", job_id))
        if isinstance(error, str) and len(error.encode("utf-8")) > 1_000:
            findings.append(_finding("error_exceeds_storage_contract", job_id))

    retrieval_verified = False
    retrieval_calls = [call for call in calls if call["tool_name"] == "knowledge_search"]
    if capability == "retrieve_knowledge" and state == "succeeded" and output is not None:
        if len(retrieval_calls) != 1:
            code = "missing_retrieval_call" if not retrieval_calls else "ambiguous_retrieval_calls"
            findings.append(_finding(code, job_id))
        else:
            call = retrieval_calls[0]
            try:
                if call["risk"] != "read_only" or call["state"] != "succeeded":
                    raise ContractError("retrieval tool call did not succeed as read_only")
                arguments = _exact_keys(
                    _load_json(call["arguments_json"], "retrieval arguments"),
                    {"query"},
                    "retrieval arguments",
                )
                result = _exact_keys(
                    _load_json(call["result_json"], "retrieval result"),
                    {"job_id", "query", "results"},
                    "retrieval result",
                )
                if result["job_id"] != job_id:
                    raise ContractError("retrieval result is bound to a different job")
                if arguments["query"] != output["query"] or result["query"] != output["query"]:
                    raise ContractError("retrieval query lineage does not match output")
                if result["results"] != output["evidence"]:
                    raise ContractError("retrieval evidence does not match the tool result")
                retrieval_verified = True
            except ContractError as exc:
                findings.append(_finding("retrieval_lineage_mismatch", job_id, str(exc)))
    elif retrieval_calls and capability != "retrieve_knowledge":
        findings.append(_finding("unexpected_retrieval_call", job_id))
    return findings, retrieval_verified


def audit_database(database: str | Path, *, min_jobs: int = 1) -> dict[str, Any]:
    if not 0 <= min_jobs <= MAX_JOBS:
        raise AuditInputError(f"min_jobs must be in [0, {MAX_JOBS}]")
    path = Path(database).resolve()
    uri = path.as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise AuditInputError(f"cannot open database read-only: {exc}") from exc
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        _validate_schema(connection)
        job_count = int(connection.execute("SELECT COUNT(*) FROM agent_jobs").fetchone()[0])
        call_count = int(connection.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0])
        if job_count > MAX_JOBS:
            raise AuditInputError(f"database contains more than {MAX_JOBS} agent jobs")
        if call_count > MAX_TOOL_CALLS:
            raise AuditInputError(f"database contains more than {MAX_TOOL_CALLS} tool calls")
        jobs = connection.execute(
            "SELECT job_id, capability, state, input_json, output_json, error, attempt, "
            "max_attempts, lease_owner, lease_until, created_at, updated_at "
            "FROM agent_jobs ORDER BY job_id"
        ).fetchall()
        calls = connection.execute(
            "SELECT call_id, job_id, tool_name, risk, arguments_json, state, result_json, "
            "requested_by, approved_by, created_at, updated_at FROM tool_calls "
            "ORDER BY job_id, call_id"
        ).fetchall()
        connection.rollback()
    except (sqlite3.Error, AuditInputError) as exc:
        connection.close()
        if isinstance(exc, AuditInputError):
            raise
        raise AuditInputError(f"cannot read audit snapshot: {exc}") from exc
    connection.close()

    calls_by_job: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for call in calls:
        calls_by_job[str(call["job_id"])].append(call)
    findings: list[dict[str, str]] = []
    state_counts: Counter[str] = Counter()
    capability_counts: Counter[str] = Counter()
    commitments: list[str] = []
    retrieval_verified = 0
    known_jobs = {str(job["job_id"]) for job in jobs}
    for job in jobs:
        job_id = str(job["job_id"])
        state_counts[str(job["state"])] += 1
        capability_counts[str(job["capability"])] += 1
        job_calls = calls_by_job.get(job_id, [])
        commitments.append(_row_commitment(job, job_calls))
        job_findings, verified = _audit_job(job, job_calls)
        findings.extend(job_findings)
        retrieval_verified += int(verified)
    for call in calls:
        if str(call["job_id"]) not in known_jobs:
            findings.append(
                {
                    "code": "orphan_tool_call",
                    "tool_ref": _sha256(str(call["call_id"]).encode("utf-8"))[:24],
                }
            )
    if job_count < min_jobs:
        findings.append({"code": "insufficient_jobs", "required": str(min_jobs)})

    report: dict[str, Any] = {
        "audited_jobs": job_count,
        "audited_tool_calls": call_count,
        "capability_counts": dict(sorted(capability_counts.items())),
        "finding_counts": dict(sorted(Counter(item["code"] for item in findings).items())),
        "findings": findings,
        "history_sha256": _sha256(_canonical_json(commitments)),
        "passed": not findings,
        "retrieval_lineages_verified": retrieval_verified,
        "schema_version": EVIDENCE_SCHEMA,
        "state_counts": dict(sorted(state_counts.items())),
    }
    report["evidence_sha256"] = _sha256(_canonical_json(report))
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit durable agent-job output contracts and retrieval lineage."
    )
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--min-jobs", type=int, default=1)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = audit_database(args.database, min_jobs=args.min_jobs)
    except AuditInputError as exc:
        print(
            json.dumps(
                {
                    "error": {"code": "invalid_input", "detail": str(exc)},
                    "passed": False,
                    "schema_version": EVIDENCE_SCHEMA,
                },
                sort_keys=True,
            )
        )
        return 3
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    sys.exit(main())
