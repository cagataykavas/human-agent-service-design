from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass

from service_desk.models import ToolRisk


@dataclass(frozen=True, slots=True)
class PlannedToolCall:
    call_id: str
    tool_name: str

    def __post_init__(self) -> None:
        if not self.call_id.strip() or not self.tool_name.strip():
            raise ValueError("call_id and tool_name are required")


@dataclass(frozen=True, slots=True)
class PlanBudget:
    max_steps: int = 8
    max_write_steps: int = 2
    max_high_impact_steps: int = 1

    def __post_init__(self) -> None:
        if self.max_steps < 1:
            raise ValueError("max_steps must be positive")
        if not 0 <= self.max_write_steps <= self.max_steps:
            raise ValueError("max_write_steps must be between zero and max_steps")
        if not 0 <= self.max_high_impact_steps <= self.max_write_steps:
            raise ValueError(
                "max_high_impact_steps must be between zero and max_write_steps"
            )


@dataclass(frozen=True, slots=True)
class PlanDecision:
    allowed: bool
    total_steps: int
    read_only_steps: int
    write_steps: int
    high_impact_steps: int
    reasons: tuple[str, ...]
    resolved_risks: tuple[tuple[str, str], ...]

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["reasons"] = list(self.reasons)
        value["resolved_risks"] = [
            {"call_id": call_id, "risk": risk} for call_id, risk in self.resolved_risks
        ]
        return value


def evaluate_plan(
    calls: tuple[PlannedToolCall, ...],
    tool_risks: Mapping[str, ToolRisk],
    budget: PlanBudget,
) -> PlanDecision:
    """Evaluate aggregate plan risk using only server-owned tool classifications."""
    reasons: list[str] = []
    call_ids = [call.call_id for call in calls]
    if len(call_ids) != len(set(call_ids)):
        reasons.append("duplicate_call_id")
    if len(calls) > budget.max_steps:
        reasons.append("step_budget_exceeded")

    resolved: list[tuple[str, str]] = []
    risks: list[ToolRisk] = []
    for call in calls:
        risk = tool_risks.get(call.tool_name)
        if risk is None:
            reasons.append(f"unknown_tool:{call.tool_name}")
            resolved.append((call.call_id, "unknown"))
            continue
        risks.append(risk)
        resolved.append((call.call_id, risk.value))

    write_steps = sum(risk is not ToolRisk.READ_ONLY for risk in risks)
    high_impact_steps = sum(risk is ToolRisk.HIGH_IMPACT for risk in risks)
    if write_steps > budget.max_write_steps:
        reasons.append("write_budget_exceeded")
    if high_impact_steps > budget.max_high_impact_steps:
        reasons.append("high_impact_budget_exceeded")

    ordered = tuple(sorted(set(reasons)))
    return PlanDecision(
        allowed=bool(calls) and not ordered,
        total_steps=len(calls),
        read_only_steps=sum(risk is ToolRisk.READ_ONLY for risk in risks),
        write_steps=write_steps,
        high_impact_steps=high_impact_steps,
        reasons=ordered if calls else ("empty_plan",),
        resolved_risks=tuple(resolved),
    )
