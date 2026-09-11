from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from service_design import (
    AgentRecommendation,
    Evidence,
    Impact,
    PolicyConfig,
    PolicyRouter,
    Recommendation,
    ReviewOutcome,
    Route,
    ServiceCase,
    summarize_outcomes,
)
from service_desk.api import create_service_desk_router
from service_desk.errors import Conflict, Forbidden, InvalidTransition, NotFound, ServiceDeskError
from service_desk.repository import SQLiteServiceDeskRepository
from service_desk.service import ServiceDesk


class EvidenceInput(BaseModel):
    evidence_id: str
    kind: str
    source: str
    quality: float = Field(ge=0.0, le=1.0)
    contradictory: bool = False
    missing_fields: list[str] = Field(default_factory=list)


class AgentRecommendationInput(BaseModel):
    action: Recommendation
    confidence: float = Field(ge=0.0, le=1.0)
    reason_codes: list[str] = Field(default_factory=list)
    explanation: str
    evidence_ids: list[str] = Field(default_factory=list)


class ServiceCaseInput(BaseModel):
    case_id: str
    customer_segment: str
    impact: Impact
    evidence: list[EvidenceInput]
    agent: AgentRecommendationInput
    customer_requested_human: bool = False
    mandatory_review: bool = False
    repeated_failure_count: int = Field(default=0, ge=0)
    deterministic_check_passed: bool = True


class PolicyInput(BaseModel):
    min_confidence_for_automation: float = Field(default=0.88, ge=0.0, le=1.0)
    min_evidence_completeness: float = Field(default=0.82, ge=0.0, le=1.0)
    high_impact_requires_human: bool = True
    repeated_failure_threshold: int = Field(default=2, ge=1)
    disagreement_requires_human: bool = True


class RouteRequest(BaseModel):
    case: ServiceCaseInput
    policy: PolicyInput = Field(default_factory=PolicyInput)


class OutcomeInput(BaseModel):
    case_id: str
    agent_action: Recommendation
    reviewer_action: Recommendation
    route: Route
    decision_seconds: float = Field(ge=0.0)
    customer_loops: int = Field(ge=0)
    explanation_present: bool


class ExperimentRequest(BaseModel):
    outcomes: list[OutcomeInput]


def _case(payload: ServiceCaseInput) -> ServiceCase:
    return ServiceCase(
        case_id=payload.case_id,
        customer_segment=payload.customer_segment,
        impact=payload.impact,
        evidence=[
            Evidence(
                evidence_id=item.evidence_id,
                kind=item.kind,
                source=item.source,
                quality=item.quality,
                contradictory=item.contradictory,
                missing_fields=tuple(item.missing_fields),
            )
            for item in payload.evidence
        ],
        agent=AgentRecommendation(
            action=payload.agent.action,
            confidence=payload.agent.confidence,
            reason_codes=tuple(payload.agent.reason_codes),
            explanation=payload.agent.explanation,
            evidence_ids=tuple(payload.agent.evidence_ids),
        ),
        customer_requested_human=payload.customer_requested_human,
        mandatory_review=payload.mandatory_review,
        repeated_failure_count=payload.repeated_failure_count,
        deterministic_check_passed=payload.deterministic_check_passed,
    )


def _config(payload: PolicyInput) -> PolicyConfig:
    return PolicyConfig(**payload.model_dump())


app = FastAPI(
    title="Human-Agent Service Design",
    version="0.2.0",
    description=(
        "A policy-routing and experiment service for prototyping human-agent collaboration. "
        "It makes automation, customer follow-up and human escalation explicit and measurable."
    ),
)

service_desk_repository = SQLiteServiceDeskRepository(
    Path(os.getenv("SERVICE_DESK_DATABASE_PATH", "service-desk.db"))
)
service_desk = ServiceDesk(service_desk_repository)
service_desk.bootstrap()
app.include_router(create_service_desk_router(service_desk))


@app.exception_handler(ServiceDeskError)
async def service_desk_error(_: Request, exc: ServiceDeskError) -> JSONResponse:
    status = 409
    if isinstance(exc, NotFound):
        status = 404
    elif isinstance(exc, Forbidden):
        status = 403
    elif isinstance(exc, InvalidTransition):
        status = 422
    elif isinstance(exc, Conflict):
        status = 409
    return JSONResponse(
        status_code=status,
        content={"error": type(exc).__name__, "detail": str(exc)},
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/route")
def route(request: RouteRequest) -> dict:
    case = _case(request.case)
    decision = PolicyRouter(_config(request.policy)).route(case)
    return {
        "case_id": case.case_id,
        "evidence_completeness": case.evidence_completeness,
        "contradictory_evidence": case.contradictory_evidence,
        "decision": asdict(decision),
    }


@app.post("/experiments/metrics")
def experiment_metrics(request: ExperimentRequest) -> dict:
    rows = [
        ReviewOutcome(
            case_id=item.case_id,
            agent_action=item.agent_action,
            reviewer_action=item.reviewer_action,
            route=item.route,
            decision_seconds=item.decision_seconds,
            customer_loops=item.customer_loops,
            explanation_present=item.explanation_present,
        )
        for item in request.outcomes
    ]
    return asdict(summarize_outcomes(rows))
