from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from service_desk.agent import AgentWorker, DeterministicAgentModel
from service_desk.models import Actor, ActorRole, AgentJobState, IssuePriority
from service_desk.repository import SQLiteServiceDeskRepository
from service_desk.run_audit import audit_database, main
from service_desk.service import ServiceDesk
from service_desk.tools import KnowledgeSearchTool, ToolRegistry


def setup_job(tmp_path: Path, capability: str = "triage"):
    database = tmp_path / "desk.db"
    repository = SQLiteServiceDeskRepository(database)
    desk = ServiceDesk(repository)
    desk.bootstrap()
    desk.create_project(key="OPS", name="Operations", project_id="project-1")
    issue = desk.create_issue(
        project_id="project-1",
        summary="Production VPN outage",
        description="Authentication is unavailable.",
        priority=IssuePriority.HIGH,
        reporter=Actor("requester-1", ActorRole.REQUESTER),
        idempotency_key="request-1",
        issue_id="issue-1",
    )
    desk.request_agent_job(
        issue.issue_id,
        capability,
        {"query": "VPN authentication runbook"}
        if capability == "retrieve_knowledge"
        else {"text": issue.summary},
        actor=Actor("agent-1", ActorRole.AGENT),
        job_id="sensitive-job-id",
    )
    return database, repository, desk


def run_worker(repository, desk, capability: str) -> None:
    tools = ToolRegistry()
    if capability == "retrieve_knowledge":
        tools.register(
            KnowledgeSearchTool(
                {
                    "private-document-id": "Confidential VPN authentication recovery steps",
                    "unrelated": "Laptop shipping policy",
                }
            )
        )
    result = AgentWorker(
        repository,
        desk,
        DeterministicAgentModel(),
        tools,
        worker_id="worker-1",
    ).run_once()
    assert result is not None


def test_valid_triage_job_passes_with_deterministic_evidence(tmp_path: Path) -> None:
    database, repository, desk = setup_job(tmp_path)
    run_worker(repository, desk, "triage")

    first = audit_database(database)
    second = audit_database(database)

    assert first["passed"] is True
    assert first["audited_jobs"] == 1
    assert first["capability_counts"] == {"triage": 1}
    assert first["evidence_sha256"] == second["evidence_sha256"]


def test_retrieval_output_is_bound_to_successful_tool_result(tmp_path: Path) -> None:
    database, repository, desk = setup_job(tmp_path, "retrieve_knowledge")
    run_worker(repository, desk, "retrieve_knowledge")

    report = audit_database(database)

    assert report["passed"] is True
    assert report["retrieval_lineages_verified"] == 1
    encoded = json.dumps(report)
    assert "sensitive-job-id" not in encoded
    assert "private-document-id" not in encoded
    assert "Confidential VPN" not in encoded


def test_mutated_retrieval_evidence_fails_lineage(tmp_path: Path) -> None:
    database, repository, desk = setup_job(tmp_path, "retrieve_knowledge")
    run_worker(repository, desk, "retrieve_knowledge")
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT output_json FROM agent_jobs WHERE job_id = 'sensitive-job-id'"
        ).fetchone()
        output = json.loads(row[0])
        output["evidence"][0]["text"] = "substituted content"
        connection.execute(
            "UPDATE agent_jobs SET output_json = ? WHERE job_id = 'sensitive-job-id'",
            (json.dumps(output, sort_keys=True, separators=(",", ":")),),
        )

    report = audit_database(database)

    assert report["passed"] is False
    assert report["finding_counts"] == {"retrieval_lineage_mismatch": 1}


def test_duplicate_and_nonfinite_output_json_fail_closed(tmp_path: Path) -> None:
    database, repository, desk = setup_job(tmp_path)
    run_worker(repository, desk, "triage")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE agent_jobs SET output_json = ? WHERE job_id = 'sensitive-job-id'",
            (
                '{"category":"incident","category":"security","priority":"high","confidence":NaN,"reason_codes":["x"]}',
            ),
        )

    report = audit_database(database)

    assert report["finding_counts"] == {"invalid_output_contract": 1}
    assert "NaN" not in json.dumps(report)


def test_terminal_job_with_lease_is_rejected(tmp_path: Path) -> None:
    database, repository, desk = setup_job(tmp_path)
    run_worker(repository, desk, "triage")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE agent_jobs SET lease_owner = 'stale-worker', lease_until = updated_at "
            "WHERE job_id = 'sensitive-job-id'"
        )

    report = audit_database(database)

    assert report["finding_counts"] == {"terminal_lease_present": 1}


def test_failed_job_cannot_retain_output_and_report_hides_error(tmp_path: Path) -> None:
    database, repository, _ = setup_job(tmp_path)
    claimed = repository.claim_agent_job("worker-1")
    assert claimed is not None
    repository.finish_agent_job(
        claimed.job_id,
        "worker-1",
        AgentJobState.FAILED,
        error="secret customer details",
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE agent_jobs SET output_json = '{}' WHERE job_id = 'sensitive-job-id'"
        )

    report = audit_database(database)

    assert report["finding_counts"] == {"unsuccessful_job_has_output": 1}
    assert "secret customer details" not in json.dumps(report)


def test_empty_history_requires_explicit_opt_out(tmp_path: Path) -> None:
    database = tmp_path / "empty.db"
    SQLiteServiceDeskRepository(database)

    rejected = audit_database(database)
    accepted = audit_database(database, min_jobs=0)

    assert rejected["finding_counts"] == {"insufficient_jobs": 1}
    assert accepted["passed"] is True


def test_audit_is_read_only(tmp_path: Path) -> None:
    database, repository, desk = setup_job(tmp_path)
    run_worker(repository, desk, "triage")
    before = database.read_bytes()

    audit_database(database)

    assert database.read_bytes() == before


def test_cli_uses_distinct_policy_and_input_exit_codes(tmp_path: Path, capsys) -> None:
    database, repository, desk = setup_job(tmp_path)
    run_worker(repository, desk, "triage")
    assert main(["--database", str(database)]) == 0
    capsys.readouterr()
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE agent_jobs SET output_json = '{}' ")
    assert main(["--database", str(database)]) == 2
    capsys.readouterr()
    assert main(["--database", str(tmp_path / "missing.db")]) == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "invalid_input"
