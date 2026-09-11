from __future__ import annotations

from dataclasses import dataclass

from human_agent.domain import Impact, Recommendation, Route, ServiceCase


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    min_confidence_for_automation: float = 0.88
    min_evidence_completeness: float = 0.82
    high_impact_requires_human: bool = True
    repeated_failure_threshold: int = 2
    disagreement_requires_human: bool = True

    def __post_init__(self) -> None:
        if not 0 <= self.min_confidence_for_automation <= 1:
            raise ValueError("automation confidence threshold must be in [0, 1]")
        if not 0 <= self.min_evidence_completeness <= 1:
            raise ValueError("evidence completeness threshold must be in [0, 1]")
        if self.repeated_failure_threshold < 1:
            raise ValueError("repeated failure threshold must be positive")


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    route: Route
    reasons: tuple[str, ...]
    customer_message: str
    reviewer_priority: int


class PolicyRouter:
    def __init__(self, config: PolicyConfig | None = None) -> None:
        self.config = config or PolicyConfig()

    def route(self, case: ServiceCase) -> RoutingDecision:
        reasons: list[str] = []
        if case.customer_requested_human:
            return self._human(["customer_requested_human"], priority=100)
        if case.mandatory_review:
            return self._human(["mandatory_policy_review"], priority=95)
        if self.config.high_impact_requires_human and case.impact is Impact.HIGH:
            reasons.append("high_impact_decision")
        if case.contradictory_evidence:
            reasons.append("contradictory_evidence")
        if self.config.disagreement_requires_human and not case.deterministic_check_passed:
            reasons.append("model_rule_disagreement")
        if case.repeated_failure_count >= self.config.repeated_failure_threshold:
            reasons.append("repeated_automation_failure")
        if reasons:
            return self._human(reasons, priority=self._priority(case, reasons))

        if case.evidence_completeness < self.config.min_evidence_completeness:
            missing = sorted(
                field_name for evidence in case.evidence for field_name in evidence.missing_fields
            )
            missing_text = ", ".join(dict.fromkeys(missing)) or "additional evidence"
            return RoutingDecision(
                route=Route.ASK_CUSTOMER,
                reasons=("insufficient_evidence",),
                customer_message=f"We need more information before continuing: {missing_text}.",
                reviewer_priority=0,
            )
        if case.agent.confidence < self.config.min_confidence_for_automation:
            return self._human(
                ["low_agent_confidence"],
                priority=self._priority(case, ["low_agent_confidence"]),
            )
        if case.agent.action is Recommendation.REQUEST_INFORMATION:
            return RoutingDecision(
                route=Route.ASK_CUSTOMER,
                reasons=("agent_requests_information",),
                customer_message=case.agent.explanation,
                reviewer_priority=0,
            )
        if case.agent.action in {Recommendation.APPROVE, Recommendation.REJECT}:
            return RoutingDecision(
                route=Route.AUTOMATE,
                reasons=("policy_allows_automation",),
                customer_message=case.agent.explanation,
                reviewer_priority=0,
            )
        return self._human(["unhandled_agent_action"], priority=80)

    @staticmethod
    def _priority(case: ServiceCase, reasons: list[str]) -> int:
        priority = 40
        priority += {Impact.LOW: 0, Impact.MEDIUM: 15, Impact.HIGH: 30}[case.impact]
        priority += 15 if "contradictory_evidence" in reasons else 0
        priority += 10 if "model_rule_disagreement" in reasons else 0
        priority += 10 if "repeated_automation_failure" in reasons else 0
        return min(priority, 100)

    @staticmethod
    def _human(reasons: list[str], *, priority: int) -> RoutingDecision:
        return RoutingDecision(
            route=Route.HUMAN_REVIEW,
            reasons=tuple(reasons),
            customer_message=(
                "Your case needs a specialist review. No action is required unless "
                "we contact you for more information."
            ),
            reviewer_priority=priority,
        )
