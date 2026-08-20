from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from statistics import mean
from typing import Iterable


class Recommendation(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    REQUEST_INFORMATION = "request_information"
    REVIEW = "review"


class Route(str, Enum):
    AUTOMATE = "automate"
    ASK_CUSTOMER = "ask_customer"
    HUMAN_REVIEW = "human_review"
    FALLBACK = "fallback"


class Impact(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    kind: str
    source: str
    quality: float
    contradictory: bool = False
    missing_fields: tuple[str, ...] = ()

    def completeness(self) -> float:
        penalty = 0.15 * len(self.missing_fields)
        contradiction_penalty = 0.35 if self.contradictory else 0.0
        return max(0.0, min(1.0, self.quality - penalty - contradiction_penalty))


@dataclass(frozen=True)
class AgentRecommendation:
    action: Recommendation
    confidence: float
    reason_codes: tuple[str, ...]
    explanation: str
    evidence_ids: tuple[str, ...]


@dataclass
class ServiceCase:
    case_id: str
    customer_segment: str
    impact: Impact
    evidence: list[Evidence]
    agent: AgentRecommendation
    customer_requested_human: bool = False
    mandatory_review: bool = False
    repeated_failure_count: int = 0
    deterministic_check_passed: bool = True
    route: Route | None = None
    route_reasons: list[str] = field(default_factory=list)

    @property
    def evidence_completeness(self) -> float:
        if not self.evidence:
            return 0.0
        return mean(item.completeness() for item in self.evidence)

    @property
    def contradictory_evidence(self) -> bool:
        return any(item.contradictory for item in self.evidence)


@dataclass(frozen=True)
class PolicyConfig:
    min_confidence_for_automation: float = 0.88
    min_evidence_completeness: float = 0.82
    high_impact_requires_human: bool = True
    repeated_failure_threshold: int = 2
    disagreement_requires_human: bool = True


@dataclass(frozen=True)
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
            reasons.append("customer_requested_human")
            return self._human(case, reasons, priority=100)

        if case.mandatory_review:
            reasons.append("mandatory_policy_review")
            return self._human(case, reasons, priority=95)

        if (
            self.config.high_impact_requires_human
            and case.impact is Impact.HIGH
        ):
            reasons.append("high_impact_decision")

        if case.contradictory_evidence:
            reasons.append("contradictory_evidence")

        if (
            self.config.disagreement_requires_human
            and not case.deterministic_check_passed
        ):
            reasons.append("model_rule_disagreement")

        if case.repeated_failure_count >= self.config.repeated_failure_threshold:
            reasons.append("repeated_automation_failure")

        if reasons:
            return self._human(case, reasons, priority=self._priority(case, reasons))

        if case.evidence_completeness < self.config.min_evidence_completeness:
            missing = sorted(
                {
                    field_name
                    for evidence in case.evidence
                    for field_name in evidence.missing_fields
                }
            )
            missing_text = ", ".join(missing) if missing else "additional evidence"
            return RoutingDecision(
                route=Route.ASK_CUSTOMER,
                reasons=("insufficient_evidence",),
                customer_message=(
                    "We need a little more information before continuing: "
                    f"{missing_text}."
                ),
                reviewer_priority=0,
            )

        if case.agent.confidence < self.config.min_confidence_for_automation:
            return self._human(
                case,
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

        return self._human(case, ["unhandled_agent_action"], priority=80)

    @staticmethod
    def _priority(case: ServiceCase, reasons: list[str]) -> int:
        priority = 40
        if case.impact is Impact.HIGH:
            priority += 30
        elif case.impact is Impact.MEDIUM:
            priority += 15
        if "contradictory_evidence" in reasons:
            priority += 15
        if "model_rule_disagreement" in reasons:
            priority += 10
        if "repeated_automation_failure" in reasons:
            priority += 10
        return min(priority, 100)

    @staticmethod
    def _human(
        case: ServiceCase,
        reasons: list[str],
        *,
        priority: int,
    ) -> RoutingDecision:
        return RoutingDecision(
            route=Route.HUMAN_REVIEW,
            reasons=tuple(reasons),
            customer_message=(
                "Your case needs a specialist review. No additional action is "
                "required unless we contact you for more information."
            ),
            reviewer_priority=priority,
        )


@dataclass(frozen=True)
class ReviewOutcome:
    case_id: str
    agent_action: Recommendation
    reviewer_action: Recommendation
    route: Route
    decision_seconds: float
    customer_loops: int
    explanation_present: bool

    @property
    def overridden(self) -> bool:
        return self.agent_action != self.reviewer_action


@dataclass(frozen=True)
class ServiceMetrics:
    cases: int
    automation_rate: float
    human_review_rate: float
    override_rate: float
    request_more_info_rate: float
    average_decision_seconds: float
    average_customer_loops: float
    explanation_coverage: float


def summarize_outcomes(outcomes: Iterable[ReviewOutcome]) -> ServiceMetrics:
    rows = list(outcomes)
    if not rows:
        return ServiceMetrics(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    cases = len(rows)
    review_rows = [row for row in rows if row.route is Route.HUMAN_REVIEW]
    return ServiceMetrics(
        cases=cases,
        automation_rate=sum(row.route is Route.AUTOMATE for row in rows) / cases,
        human_review_rate=len(review_rows) / cases,
        override_rate=(
            sum(row.overridden for row in review_rows) / len(review_rows)
            if review_rows
            else 0.0
        ),
        request_more_info_rate=(
            sum(row.route is Route.ASK_CUSTOMER for row in rows) / cases
        ),
        average_decision_seconds=mean(row.decision_seconds for row in rows),
        average_customer_loops=mean(row.customer_loops for row in rows),
        explanation_coverage=(
            sum(row.explanation_present for row in rows) / cases
        ),
    )


def demo_cases() -> list[ServiceCase]:
    return [
        ServiceCase(
            case_id="case-001",
            customer_segment="retail",
            impact=Impact.LOW,
            evidence=[
                Evidence("id-1", "identity", "document", quality=0.97),
                Evidence("addr-1", "address", "registry", quality=0.95),
            ],
            agent=AgentRecommendation(
                Recommendation.APPROVE,
                confidence=0.94,
                reason_codes=("identity_match", "address_match"),
                explanation="Your information was verified successfully.",
                evidence_ids=("id-1", "addr-1"),
            ),
        ),
        ServiceCase(
            case_id="case-002",
            customer_segment="retail",
            impact=Impact.MEDIUM,
            evidence=[
                Evidence(
                    "income-1",
                    "income",
                    "customer_upload",
                    quality=0.78,
                    missing_fields=("employer_name",),
                )
            ],
            agent=AgentRecommendation(
                Recommendation.REQUEST_INFORMATION,
                confidence=0.89,
                reason_codes=("income_incomplete",),
                explanation="Please provide your employer name to continue.",
                evidence_ids=("income-1",),
            ),
        ),
        ServiceCase(
            case_id="case-003",
            customer_segment="business",
            impact=Impact.HIGH,
            evidence=[
                Evidence(
                    "ubo-1",
                    "ownership",
                    "registry",
                    quality=0.92,
                    contradictory=True,
                ),
                Evidence("ubo-2", "ownership", "customer_upload", quality=0.88),
            ],
            agent=AgentRecommendation(
                Recommendation.REVIEW,
                confidence=0.74,
                reason_codes=("ownership_conflict",),
                explanation="Ownership information requires specialist review.",
                evidence_ids=("ubo-1", "ubo-2"),
            ),
            mandatory_review=True,
        ),
    ]


def main() -> None:
    router = PolicyRouter()
    for case in demo_cases():
        decision = router.route(case)
        case.route = decision.route
        case.route_reasons[:] = decision.reasons
        print(
            {
                "case_id": case.case_id,
                "route": decision.route.value,
                "reasons": decision.reasons,
                "priority": decision.reviewer_priority,
                "message": decision.customer_message,
            }
        )


if __name__ == "__main__":
    main()
