from service_design import (
    AgentRecommendation,
    Evidence,
    Impact,
    PolicyRouter,
    Recommendation,
    ReviewOutcome,
    Route,
    ServiceCase,
    summarize_outcomes,
)


def make_case(*, confidence: float = 0.95, impact: Impact = Impact.LOW) -> ServiceCase:
    return ServiceCase(
        case_id="case-test",
        customer_segment="retail",
        impact=impact,
        evidence=[Evidence("id", "identity", "demo", quality=0.98)],
        agent=AgentRecommendation(
            Recommendation.APPROVE,
            confidence=confidence,
            reason_codes=("ok",),
            explanation="Approved.",
            evidence_ids=("id",),
        ),
    )


def test_low_risk_high_confidence_can_automate():
    decision = PolicyRouter().route(make_case())
    assert decision.route is Route.AUTOMATE


def test_high_impact_routes_to_human():
    decision = PolicyRouter().route(make_case(impact=Impact.HIGH))
    assert decision.route is Route.HUMAN_REVIEW
    assert "high_impact_decision" in decision.reasons


def test_low_confidence_routes_to_human():
    decision = PolicyRouter().route(make_case(confidence=0.50))
    assert decision.route is Route.HUMAN_REVIEW
    assert "low_agent_confidence" in decision.reasons


def test_customer_request_always_routes_to_human():
    case = make_case()
    case.customer_requested_human = True
    decision = PolicyRouter().route(case)
    assert decision.route is Route.HUMAN_REVIEW
    assert decision.reviewer_priority == 100


def test_service_metrics_count_overrides():
    metrics = summarize_outcomes(
        [
            ReviewOutcome(
                "1",
                Recommendation.APPROVE,
                Recommendation.REJECT,
                Route.HUMAN_REVIEW,
                20,
                0,
                True,
            ),
            ReviewOutcome(
                "2",
                Recommendation.APPROVE,
                Recommendation.APPROVE,
                Route.AUTOMATE,
                2,
                0,
                True,
            ),
        ]
    )
    assert metrics.cases == 2
    assert metrics.automation_rate == 0.5
    assert metrics.override_rate == 1.0
