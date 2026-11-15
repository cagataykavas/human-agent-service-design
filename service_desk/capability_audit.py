"""Audit agent tool traces against a reviewed least-privilege manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

MAX_INPUT_BYTES = 10 * 1024 * 1024
MAX_RUNS = 100_000
MAX_CALLS_PER_RUN = 1_000
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


@dataclass(frozen=True, slots=True)
class CapabilityGrant:
    capability: str
    allowed_tools: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AgentRunEvidence:
    run_id: str
    capability: str
    tool_calls: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CapabilityAuditPolicy:
    maximum_tool_calls_per_run: int = 16
    minimum_runs_per_capability: int = 1
    maximum_retained_findings: int = 100

    def __post_init__(self) -> None:
        for name in (
            "maximum_tool_calls_per_run",
            "minimum_runs_per_capability",
            "maximum_retained_findings",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < 1:
                raise ValueError(f"{name} must be positive")
        if self.maximum_tool_calls_per_run > MAX_CALLS_PER_RUN:
            raise ValueError(f"maximum_tool_calls_per_run cannot exceed {MAX_CALLS_PER_RUN}")
        if self.maximum_retained_findings > 1_000:
            raise ValueError("maximum_retained_findings cannot exceed 1000")


@dataclass(frozen=True, slots=True)
class CapabilityFinding:
    code: str
    capability: str
    run_ref: str | None
    tool: str | None
    detail: str


@dataclass(frozen=True, slots=True)
class CapabilitySummary:
    capability: str
    evaluated_runs: int
    observed_calls: int
    observed_tools: tuple[str, ...]
    unexercised_grants: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CapabilityAuditReport:
    decision: str
    evidence_digest: str
    evaluated_runs: int
    evaluated_capabilities: int
    policy: CapabilityAuditPolicy
    findings: tuple[CapabilityFinding, ...]
    truncated_findings: int
    summaries: tuple[CapabilitySummary, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _validate_name(kind: str, value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{kind} must be a string")
    if not NAME_PATTERN.fullmatch(value):
        raise ValueError(f"{kind} must match {NAME_PATTERN.pattern!r}; received {value!r}")
    return value


def _validate_contract(
    known_tools: Sequence[str],
    grants: Sequence[CapabilityGrant],
    runs: Sequence[AgentRunEvidence],
) -> tuple[tuple[str, ...], tuple[CapabilityGrant, ...], tuple[AgentRunEvidence, ...]]:
    if (
        isinstance(known_tools, (str, bytes))
        or isinstance(grants, (str, bytes))
        or isinstance(runs, (str, bytes))
    ):
        raise TypeError("known_tools, grants and runs must be sequences")
    if not known_tools:
        raise ValueError("known_tools must not be empty")
    if not grants:
        raise ValueError("grants must not be empty")
    if not runs:
        raise ValueError("runs must not be empty")
    if len(runs) > MAX_RUNS:
        raise ValueError(f"runs exceed the {MAX_RUNS} record limit")

    tools = tuple(_validate_name("tool name", item) for item in known_tools)
    if len(tools) != len(set(tools)):
        raise ValueError("known_tools contains duplicates")
    known_tool_set = set(tools)

    validated_grants: list[CapabilityGrant] = []
    seen_capabilities: set[str] = set()
    for index, grant in enumerate(grants):
        if not isinstance(grant, CapabilityGrant):
            raise TypeError(f"grants[{index}] has the wrong type")
        capability = _validate_name("capability", grant.capability)
        if capability in seen_capabilities:
            raise ValueError(f"duplicate capability grant: {capability}")
        seen_capabilities.add(capability)
        if isinstance(grant.allowed_tools, (str, bytes)):
            raise TypeError(f"grant {capability!r} tools must be a sequence")
        allowed = tuple(_validate_name("allowed tool", tool) for tool in grant.allowed_tools)
        if len(allowed) != len(set(allowed)):
            raise ValueError(f"grant {capability!r} contains duplicate tools")
        unknown = sorted(set(allowed) - known_tool_set)
        if unknown:
            raise ValueError(f"grant {capability!r} references unknown tools: {unknown}")
        validated_grants.append(CapabilityGrant(capability, tuple(sorted(allowed))))

    validated_runs: list[AgentRunEvidence] = []
    seen_runs: set[str] = set()
    for index, run in enumerate(runs):
        if not isinstance(run, AgentRunEvidence):
            raise TypeError(f"runs[{index}] has the wrong type")
        if not isinstance(run.run_id, str):
            raise TypeError(f"runs[{index}].run_id must be a string")
        if not run.run_id.strip() or len(run.run_id) > 256:
            raise ValueError(f"runs[{index}].run_id must contain 1-256 characters")
        if run.run_id in seen_runs:
            raise ValueError(f"duplicate run_id: {run.run_id}")
        seen_runs.add(run.run_id)
        capability = _validate_name("run capability", run.capability)
        if isinstance(run.tool_calls, (str, bytes)):
            raise TypeError(f"runs[{index}].tool_calls must be a sequence")
        if len(run.tool_calls) > MAX_CALLS_PER_RUN:
            raise ValueError(f"runs[{index}].tool_calls exceeds the {MAX_CALLS_PER_RUN} call limit")
        calls = tuple(_validate_name("tool call", tool) for tool in run.tool_calls)
        validated_runs.append(AgentRunEvidence(run.run_id, capability, calls))

    return tuple(sorted(tools)), tuple(validated_grants), tuple(validated_runs)


def _run_ref(run_id: str) -> str:
    return hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:16]


def _evidence_digest(
    known_tools: Sequence[str],
    grants: Sequence[CapabilityGrant],
    runs: Sequence[AgentRunEvidence],
    policy: CapabilityAuditPolicy,
) -> str:
    payload = {
        "schema_version": 1,
        "known_tools": sorted(known_tools),
        "grants": [asdict(grant) for grant in sorted(grants, key=lambda item: item.capability)],
        "runs": [asdict(run) for run in sorted(runs, key=lambda item: item.run_id)],
        "policy": asdict(policy),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def audit_capability_usage(
    known_tools: Sequence[str],
    grants: Sequence[CapabilityGrant],
    runs: Sequence[AgentRunEvidence],
    policy: CapabilityAuditPolicy | None = None,
) -> CapabilityAuditReport:
    """Compare observed calls with a reviewed capability/tool manifest.

    Findings are retained up to a policy-owned bound, but all breaches still
    contribute to ``truncated_findings`` and fail the decision.
    """

    policy = policy or CapabilityAuditPolicy()
    tools, admitted_grants, admitted_runs = _validate_contract(known_tools, grants, runs)
    known_tool_set = set(tools)
    grant_map = {grant.capability: set(grant.allowed_tools) for grant in admitted_grants}
    findings: list[CapabilityFinding] = []
    total_findings = 0

    def add_finding(finding: CapabilityFinding) -> None:
        nonlocal total_findings
        total_findings += 1
        if len(findings) < policy.maximum_retained_findings:
            findings.append(finding)

    runs_by_capability: dict[str, list[AgentRunEvidence]] = {
        capability: [] for capability in grant_map
    }
    observed_by_capability: dict[str, set[str]] = {capability: set() for capability in grant_map}

    for run in admitted_runs:
        if run.capability not in grant_map:
            add_finding(
                CapabilityFinding(
                    code="unknown_capability",
                    capability=run.capability,
                    run_ref=_run_ref(run.run_id),
                    tool=None,
                    detail="run capability is absent from the reviewed manifest",
                )
            )
            continue
        runs_by_capability[run.capability].append(run)
        if len(run.tool_calls) > policy.maximum_tool_calls_per_run:
            add_finding(
                CapabilityFinding(
                    code="tool_call_budget_exceeded",
                    capability=run.capability,
                    run_ref=_run_ref(run.run_id),
                    tool=None,
                    detail=(
                        f"observed {len(run.tool_calls)} calls; policy allows "
                        f"{policy.maximum_tool_calls_per_run}"
                    ),
                )
            )
        for tool in sorted(set(run.tool_calls)):
            observed_by_capability[run.capability].add(tool)
            if tool not in known_tool_set:
                code = "unknown_tool"
                detail = "tool is absent from the reviewed registry"
            elif tool not in grant_map[run.capability]:
                code = "tool_not_granted"
                detail = "tool is not granted to this capability"
            else:
                continue
            add_finding(
                CapabilityFinding(
                    code=code,
                    capability=run.capability,
                    run_ref=_run_ref(run.run_id),
                    tool=tool,
                    detail=detail,
                )
            )

    summaries: list[CapabilitySummary] = []
    for capability in sorted(grant_map):
        capability_runs = runs_by_capability[capability]
        if len(capability_runs) < policy.minimum_runs_per_capability:
            add_finding(
                CapabilityFinding(
                    code="capability_coverage_below_minimum",
                    capability=capability,
                    run_ref=None,
                    tool=None,
                    detail=(
                        f"observed {len(capability_runs)} runs; policy requires "
                        f"{policy.minimum_runs_per_capability}"
                    ),
                )
            )
        observed = observed_by_capability[capability]
        summaries.append(
            CapabilitySummary(
                capability=capability,
                evaluated_runs=len(capability_runs),
                observed_calls=sum(len(run.tool_calls) for run in capability_runs),
                observed_tools=tuple(sorted(observed)),
                unexercised_grants=tuple(sorted(grant_map[capability] - observed)),
            )
        )

    return CapabilityAuditReport(
        decision="fail" if total_findings else "pass",
        evidence_digest=_evidence_digest(tools, admitted_grants, admitted_runs, policy),
        evaluated_runs=len(admitted_runs),
        evaluated_capabilities=len(admitted_grants),
        policy=policy,
        findings=tuple(findings),
        truncated_findings=total_findings - len(findings),
        summaries=tuple(summaries),
    )


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_evidence(
    path: Path,
) -> tuple[list[str], list[CapabilityGrant], list[AgentRunEvidence]]:
    if path.is_symlink():
        raise ValueError(f"refusing symlink input: {path}")
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError(f"input exceeds {MAX_INPUT_BYTES} bytes: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    if not isinstance(payload, dict) or set(payload) != {"known_tools", "grants", "runs"}:
        raise ValueError("input must contain exactly known_tools, grants and runs")
    if not isinstance(payload["known_tools"], list):
        raise TypeError("known_tools must be an array")
    if not isinstance(payload["grants"], dict):
        raise TypeError("grants must be an object mapping capabilities to tool arrays")
    if not isinstance(payload["runs"], list):
        raise TypeError("runs must be an array")

    grants: list[CapabilityGrant] = []
    for capability, allowed_tools in payload["grants"].items():
        if not isinstance(allowed_tools, list):
            raise TypeError(f"grant {capability!r} must be an array")
        grants.append(CapabilityGrant(capability, tuple(allowed_tools)))

    runs: list[AgentRunEvidence] = []
    expected_run_fields = {"run_id", "capability", "tool_calls"}
    for index, row in enumerate(payload["runs"]):
        if not isinstance(row, dict) or set(row) != expected_run_fields:
            raise ValueError(f"runs[{index}] must contain exactly {sorted(expected_run_fields)}")
        if not isinstance(row["tool_calls"], list):
            raise TypeError(f"runs[{index}].tool_calls must be an array")
        runs.append(
            AgentRunEvidence(
                run_id=row["run_id"],
                capability=row["capability"],
                tool_calls=tuple(row["tool_calls"]),
            )
        )
    return payload["known_tools"], grants, runs


def _write_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit agent capability/tool least privilege")
    parser.add_argument("input", type=Path, help="reviewed manifest and run evidence JSON")
    parser.add_argument("--output", type=Path, required=True, help="atomic JSON report path")
    parser.add_argument("--maximum-tool-calls-per-run", type=int, default=16)
    parser.add_argument("--minimum-runs-per-capability", type=int, default=1)
    parser.add_argument("--maximum-retained-findings", type=int, default=100)
    args = parser.parse_args(argv)

    try:
        policy = CapabilityAuditPolicy(
            maximum_tool_calls_per_run=args.maximum_tool_calls_per_run,
            minimum_runs_per_capability=args.minimum_runs_per_capability,
            maximum_retained_findings=args.maximum_retained_findings,
        )
        known_tools, grants, runs = _load_evidence(args.input)
        report = audit_capability_usage(known_tools, grants, runs, policy)
        _write_atomic(args.output, report.to_dict())
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"capability audit failed: {exc}", file=sys.stderr)
        return 3
    return 0 if report.decision == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
