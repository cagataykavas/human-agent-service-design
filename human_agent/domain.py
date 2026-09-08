from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from statistics import mean


class Recommendation(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    REQUEST_INFORMATION = "request_information"
    REVIEW = "review"


class Route(StrEnum):
    AUTOMATE = "automate"
    ASK_CUSTOMER = "ask_customer"
    HUMAN_REVIEW = "human_review"
    FALLBACK = "fallback"


class Impact(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class Evidence:
    evidence_id: str
    kind: str
    source: str
    quality: float
    contradictory: bool = False
    missing_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.evidence_id:
            raise ValueError("evidence_id must not be empty")
        if not 0 <= self.quality <= 1:
            raise ValueError("quality must be between zero and one")

    def completeness(self) -> float:
        penalty = 0.15 * len(self.missing_fields)
        contradiction_penalty = 0.35 if self.contradictory else 0.0
        return max(0.0, min(1.0, self.quality - penalty - contradiction_penalty))


@dataclass(frozen=True, slots=True)
class AgentRecommendation:
    action: Recommendation
    confidence: float
    reason_codes: tuple[str, ...]
    explanation: str
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between zero and one")
        if not self.explanation.strip():
            raise ValueError("explanation must not be empty")


@dataclass(slots=True)
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

    def __post_init__(self) -> None:
        if not self.case_id:
            raise ValueError("case_id must not be empty")
        if self.repeated_failure_count < 0:
            raise ValueError("repeated_failure_count must be non-negative")
        known_ids = {item.evidence_id for item in self.evidence}
        unknown = set(self.agent.evidence_ids) - known_ids
        if unknown:
            raise ValueError(f"recommendation references unknown evidence: {sorted(unknown)}")

    @property
    def evidence_completeness(self) -> float:
        if not self.evidence:
            return 0.0
        return mean(item.completeness() for item in self.evidence)

    @property
    def contradictory_evidence(self) -> bool:
        return any(item.contradictory for item in self.evidence)
