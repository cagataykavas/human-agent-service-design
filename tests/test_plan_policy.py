import pytest

from service_desk.models import ToolRisk
from service_desk.plan_policy import PlanBudget, PlannedToolCall, evaluate_plan

RISKS = {
    "knowledge_search": ToolRisk.READ_ONLY,
    "service_desk_sql": ToolRisk.READ_ONLY,
    "assign_issue": ToolRisk.REVERSIBLE_WRITE,
    "transition_issue": ToolRisk.HIGH_IMPACT,
}


def call(number, tool):
    return PlannedToolCall(f"call-{number}", tool)


def test_allows_bounded_plan_and_reports_server_owned_risk():
    decision = evaluate_plan(
        (call(1, "knowledge_search"), call(2, "transition_issue")),
        RISKS,
        PlanBudget(),
    )
    assert decision.allowed
    assert decision.read_only_steps == 1
    assert decision.write_steps == 1
    assert decision.high_impact_steps == 1
    assert decision.to_dict()["resolved_risks"][1] == {
        "call_id": "call-2",
        "risk": "high_impact",
    }


def test_rejects_aggregate_write_and_high_impact_risk():
    decision = evaluate_plan(
        (
            call(1, "transition_issue"),
            call(2, "assign_issue"),
            call(3, "transition_issue"),
        ),
        RISKS,
        PlanBudget(max_steps=5, max_write_steps=1, max_high_impact_steps=1),
    )
    assert not decision.allowed
    assert decision.reasons == (
        "high_impact_budget_exceeded",
        "write_budget_exceeded",
    )


def test_unknown_tool_and_duplicate_call_ids_fail_closed():
    decision = evaluate_plan(
        (PlannedToolCall("same", "knowledge_search"), PlannedToolCall("same", "shell")),
        RISKS,
        PlanBudget(),
    )
    assert not decision.allowed
    assert decision.reasons == ("duplicate_call_id", "unknown_tool:shell")
    assert decision.resolved_risks[-1] == ("same", "unknown")


def test_empty_and_oversized_plans_are_rejected():
    assert evaluate_plan((), RISKS, PlanBudget()).reasons == ("empty_plan",)
    oversized = tuple(call(index, "knowledge_search") for index in range(3))
    assert evaluate_plan(oversized, RISKS, PlanBudget(max_steps=2)).reasons == (
        "step_budget_exceeded",
    )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: PlannedToolCall("", "tool"),
        lambda: PlannedToolCall("id", ""),
        lambda: PlanBudget(max_steps=0),
        lambda: PlanBudget(max_steps=1, max_write_steps=2),
        lambda: PlanBudget(max_write_steps=0, max_high_impact_steps=1),
    ],
)
def test_invalid_policy_inputs_fail_closed(factory):
    with pytest.raises(ValueError):
        factory()
