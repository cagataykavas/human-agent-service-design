from __future__ import annotations

from dataclasses import dataclass
from statistics import mean
from typing import Iterable

from human_agent.domain import Recommendation, Route


@dataclass(frozen=True, slots=True)
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


@dataclass(frozen=True, slots=True)
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
    review_rows = [row for row in rows if row.route is Route.HUMAN_REVIEW]
    cases = len(rows)
    return ServiceMetrics(
        cases=cases,
        automation_rate=sum(row.route is Route.AUTOMATE for row in rows) / cases,
        human_review_rate=len(review_rows) / cases,
        override_rate=(
            sum(row.overridden for row in review_rows) / len(review_rows)
            if review_rows
            else 0.0
        ),
        request_more_info_rate=sum(row.route is Route.ASK_CUSTOMER for row in rows) / cases,
        average_decision_seconds=mean(row.decision_seconds for row in rows),
        average_customer_loops=mean(row.customer_loops for row in rows),
        explanation_coverage=sum(row.explanation_present for row in rows) / cases,
    )
