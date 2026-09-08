"""Backward-compatible facade for the original single-module API.

The implementation now lives in the ``human_agent`` package. Existing examples can
keep importing this module while new integrations use explicit package boundaries.
"""

from human_agent.domain import (
    AgentRecommendation,
    Evidence,
    Impact,
    Recommendation,
    Route,
    ServiceCase,
)
from human_agent.metrics import ReviewOutcome, ServiceMetrics, summarize_outcomes
from human_agent.policy import PolicyConfig, PolicyRouter, RoutingDecision


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


__all__ = [
    "AgentRecommendation",
    "Evidence",
    "Impact",
    "PolicyConfig",
    "PolicyRouter",
    "Recommendation",
    "ReviewOutcome",
    "Route",
    "RoutingDecision",
    "ServiceCase",
    "ServiceMetrics",
    "demo_cases",
    "summarize_outcomes",
]


if __name__ == "__main__":
    main()
