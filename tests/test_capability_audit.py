import json
from pathlib import Path

import pytest

from service_desk.capability_audit import (
    AgentRunEvidence,
    CapabilityAuditPolicy,
    CapabilityGrant,
    audit_capability_usage,
    main,
)

KNOWN_TOOLS = ("knowledge_search", "service_desk_sql", "transition_issue")
GRANTS = (
    CapabilityGrant("retrieve_knowledge", ("knowledge_search",)),
    CapabilityGrant("summarize", ()),
    CapabilityGrant("triage", ("knowledge_search", "service_desk_sql")),
)


def passing_runs() -> list[AgentRunEvidence]:
    return [
        AgentRunEvidence("run-1", "retrieve_knowledge", ("knowledge_search",)),
        AgentRunEvidence("run-2", "summarize", ()),
        AgentRunEvidence("run-3", "triage", ("knowledge_search", "service_desk_sql")),
    ]


def test_reviewed_capability_usage_passes() -> None:
    report = audit_capability_usage(KNOWN_TOOLS, GRANTS, passing_runs())

    assert report.decision == "pass"
    assert report.findings == ()
    assert report.truncated_findings == 0
    assert len(report.evidence_digest) == 64


def test_known_but_ungranted_tool_fails() -> None:
    runs = passing_runs()
    runs[0] = AgentRunEvidence("run-1", "retrieve_knowledge", ("service_desk_sql",))

    report = audit_capability_usage(KNOWN_TOOLS, GRANTS, runs)

    assert report.decision == "fail"
    assert report.findings[0].code == "tool_not_granted"
    assert report.findings[0].run_ref != "run-1"


def test_unknown_tool_fails() -> None:
    runs = passing_runs()
    runs[0] = AgentRunEvidence("run-1", "retrieve_knowledge", ("shell_exec",))

    report = audit_capability_usage(KNOWN_TOOLS, GRANTS, runs)

    assert {finding.code for finding in report.findings} == {"unknown_tool"}


def test_unknown_capability_fails_without_raw_run_identifier() -> None:
    runs = passing_runs() + [AgentRunEvidence("sensitive-run-id", "delete_all", ())]

    report = audit_capability_usage(KNOWN_TOOLS, GRANTS, runs)

    finding = next(item for item in report.findings if item.code == "unknown_capability")
    assert finding.run_ref is not None
    assert "sensitive-run-id" not in json.dumps(report.to_dict())


def test_tool_call_budget_is_enforced() -> None:
    runs = passing_runs()
    runs[0] = AgentRunEvidence("run-1", "retrieve_knowledge", ("knowledge_search",) * 3)
    policy = CapabilityAuditPolicy(maximum_tool_calls_per_run=2)

    report = audit_capability_usage(KNOWN_TOOLS, GRANTS, runs, policy)

    assert "tool_call_budget_exceeded" in {item.code for item in report.findings}


def test_every_declared_capability_requires_coverage() -> None:
    report = audit_capability_usage(KNOWN_TOOLS, GRANTS, passing_runs()[:2])

    finding = next(
        item for item in report.findings if item.code == "capability_coverage_below_minimum"
    )
    assert finding.capability == "triage"


def test_unexercised_grants_are_reported_without_claiming_a_breach() -> None:
    runs = passing_runs()
    runs[-1] = AgentRunEvidence("run-3", "triage", ("knowledge_search",))

    report = audit_capability_usage(KNOWN_TOOLS, GRANTS, runs)

    triage = next(item for item in report.summaries if item.capability == "triage")
    assert report.decision == "pass"
    assert triage.unexercised_grants == ("service_desk_sql",)


def test_digest_is_invariant_to_manifest_and_run_order() -> None:
    forward = audit_capability_usage(KNOWN_TOOLS, GRANTS, passing_runs())
    reverse = audit_capability_usage(
        tuple(reversed(KNOWN_TOOLS)), tuple(reversed(GRANTS)), list(reversed(passing_runs()))
    )

    assert forward.evidence_digest == reverse.evidence_digest


def test_digest_binds_policy() -> None:
    first = audit_capability_usage(KNOWN_TOOLS, GRANTS, passing_runs())
    second = audit_capability_usage(
        KNOWN_TOOLS,
        GRANTS,
        passing_runs(),
        CapabilityAuditPolicy(maximum_tool_calls_per_run=15),
    )

    assert first.evidence_digest != second.evidence_digest


def test_findings_are_bounded_but_all_breaches_fail() -> None:
    runs = [
        AgentRunEvidence(f"bad-run-{index}", "retrieve_knowledge", ("shell_exec",))
        for index in range(8)
    ]
    runs.extend(passing_runs()[1:])
    policy = CapabilityAuditPolicy(maximum_retained_findings=3)

    report = audit_capability_usage(KNOWN_TOOLS, GRANTS, runs, policy)

    assert report.decision == "fail"
    assert len(report.findings) == 3
    assert report.truncated_findings == 5


@pytest.mark.parametrize(
    "known_tools,grants,runs,match",
    [
        (KNOWN_TOOLS, GRANTS, [], "runs must not be empty"),
        (KNOWN_TOOLS, GRANTS, [AgentRunEvidence("run", "triage", ())] * 2, "duplicate run_id"),
        (("knowledge_search", "knowledge_search"), GRANTS, passing_runs(), "duplicates"),
        (KNOWN_TOOLS, (CapabilityGrant("Bad Name", ()),), passing_runs(), "must match"),
        (KNOWN_TOOLS, (CapabilityGrant("triage", ("unknown",)),), passing_runs(), "unknown tools"),
    ],
)
def test_malformed_contract_fails_closed(
    known_tools: tuple[str, ...],
    grants: tuple[CapabilityGrant, ...],
    runs: list[AgentRunEvidence],
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        audit_capability_usage(known_tools, grants, runs)


def test_string_call_collections_fail_closed() -> None:
    with pytest.raises(TypeError, match="tools must be a sequence"):
        audit_capability_usage(
            KNOWN_TOOLS,
            (CapabilityGrant("triage", "knowledge_search"),),  # type: ignore[arg-type]
            [AgentRunEvidence("run", "triage", ())],
        )
    with pytest.raises(TypeError, match="tool_calls must be a sequence"):
        audit_capability_usage(
            KNOWN_TOOLS,
            (CapabilityGrant("triage", ("knowledge_search",)),),
            [AgentRunEvidence("run", "triage", "knowledge_search")],  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"maximum_tool_calls_per_run": True},
        {"minimum_runs_per_capability": 0},
        {"maximum_retained_findings": 1001},
    ],
)
def test_invalid_policy_fails_closed(kwargs: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        CapabilityAuditPolicy(**kwargs)


def write_evidence(path: Path, runs: list[AgentRunEvidence]) -> None:
    path.write_text(
        json.dumps(
            {
                "known_tools": list(KNOWN_TOOLS),
                "grants": {grant.capability: list(grant.allowed_tools) for grant in GRANTS},
                "runs": [
                    {
                        "run_id": run.run_id,
                        "capability": run.capability,
                        "tool_calls": list(run.tool_calls),
                    }
                    for run in runs
                ],
            }
        ),
        encoding="utf-8",
    )


def test_cli_writes_passing_report(tmp_path: Path) -> None:
    input_path = tmp_path / "evidence.json"
    output_path = tmp_path / "report.json"
    write_evidence(input_path, passing_runs())

    exit_code = main([str(input_path), "--output", str(output_path)])

    assert exit_code == 0
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["decision"] == "pass"
    assert payload["evaluated_capabilities"] == 3


def test_cli_returns_distinct_policy_failure(tmp_path: Path) -> None:
    input_path = tmp_path / "evidence.json"
    output_path = tmp_path / "report.json"
    runs = passing_runs()
    runs[0] = AgentRunEvidence("run-1", "retrieve_knowledge", ("transition_issue",))
    write_evidence(input_path, runs)

    exit_code = main([str(input_path), "--output", str(output_path)])

    assert exit_code == 2
    assert json.loads(output_path.read_text(encoding="utf-8"))["decision"] == "fail"


def test_cli_rejects_duplicate_json_fields_without_output(tmp_path: Path) -> None:
    input_path = tmp_path / "evidence.json"
    output_path = tmp_path / "report.json"
    input_path.write_text(
        '{"known_tools":[],"known_tools":[],"grants":{},"runs":[]}',
        encoding="utf-8",
    )

    exit_code = main([str(input_path), "--output", str(output_path)])

    assert exit_code == 3
    assert not output_path.exists()
