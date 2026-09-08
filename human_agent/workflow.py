from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import StrEnum

from human_agent.audit import AuditLedger
from human_agent.domain import Recommendation, Route, ServiceCase
from human_agent.policy import PolicyRouter, RoutingDecision
from human_agent.review_queue import ReviewQueue


class CaseState(StrEnum):
    RECEIVED = "received"
    AWAITING_CUSTOMER = "awaiting_customer"
    QUEUED_FOR_REVIEW = "queued_for_review"
    UNDER_REVIEW = "under_review"
    DECIDED = "decided"


@dataclass(slots=True)
class CaseRecord:
    case: ServiceCase
    state: CaseState = CaseState.RECEIVED
    version: int = 1
    decision: Recommendation | None = None
    assigned_reviewer: str | None = None


class VersionConflict(RuntimeError):
    """Raised when a caller attempts to mutate a stale case snapshot."""


class InvalidTransition(RuntimeError):
    """Raised when a workflow command is invalid for the current state."""


class CaseWorkflow:
    """In-memory orchestration model with audit and optimistic concurrency semantics."""

    def __init__(
        self,
        *,
        router: PolicyRouter | None = None,
        ledger: AuditLedger | None = None,
        review_queue: ReviewQueue | None = None,
    ) -> None:
        self.router = router if router is not None else PolicyRouter()
        self.ledger = ledger if ledger is not None else AuditLedger()
        self.review_queue = review_queue if review_queue is not None else ReviewQueue()
        self._records: dict[str, CaseRecord] = {}
        self._lock = threading.RLock()

    def submit(self, case: ServiceCase) -> CaseRecord:
        with self._lock:
            if case.case_id in self._records:
                raise ValueError(f"case {case.case_id!r} already exists")
            record = CaseRecord(case=case)
            self._records[case.case_id] = record
            self.ledger.append(case.case_id, "case_received", "customer")
            return record

    def route(self, case_id: str, *, expected_version: int) -> RoutingDecision:
        with self._lock:
            record = self._get(case_id)
            self._expect_version(record, expected_version)
            if record.state is not CaseState.RECEIVED:
                raise InvalidTransition(f"cannot route case in {record.state.value}")
            decision = self.router.route(record.case)
            record.case.route = decision.route
            record.case.route_reasons[:] = decision.reasons
            if decision.route is Route.HUMAN_REVIEW:
                record.state = CaseState.QUEUED_FOR_REVIEW
                self.review_queue.enqueue(
                    case_id,
                    decision.reviewer_priority,
                    decision.reasons,
                )
            elif decision.route is Route.ASK_CUSTOMER:
                record.state = CaseState.AWAITING_CUSTOMER
            elif decision.route is Route.AUTOMATE:
                record.state = CaseState.DECIDED
                record.decision = record.case.agent.action
            else:
                raise InvalidTransition("fallback route requires an external recovery handler")
            record.version += 1
            self.ledger.append(
                case_id,
                "case_routed",
                "policy_router",
                {
                    "route": decision.route.value,
                    "reasons": list(decision.reasons),
                    "priority": decision.reviewer_priority,
                    "version": record.version,
                },
            )
            return decision

    def accept_review(
        self,
        reviewer_id: str,
        *,
        expected_version: int | None = None,
    ) -> CaseRecord | None:
        with self._lock:
            item = self.review_queue.lease(reviewer_id)
            if item is None:
                return None
            record = self._get(item.case_id)
            if expected_version is not None:
                self._expect_version(record, expected_version)
            if record.state is not CaseState.QUEUED_FOR_REVIEW:
                raise InvalidTransition(f"cannot accept case in {record.state.value}")
            record.state = CaseState.UNDER_REVIEW
            record.assigned_reviewer = reviewer_id
            record.version += 1
            self.ledger.append(
                record.case.case_id,
                "review_started",
                reviewer_id,
                {"version": record.version},
            )
            return record

    def decide(
        self,
        case_id: str,
        reviewer_id: str,
        decision: Recommendation,
        *,
        expected_version: int,
        rationale: str,
    ) -> CaseRecord:
        with self._lock:
            record = self._get(case_id)
            self._expect_version(record, expected_version)
            if record.state is not CaseState.UNDER_REVIEW:
                raise InvalidTransition(f"cannot decide case in {record.state.value}")
            if record.assigned_reviewer != reviewer_id:
                raise PermissionError("case is assigned to another reviewer")
            if decision not in {Recommendation.APPROVE, Recommendation.REJECT}:
                raise ValueError("reviewer decision must be approve or reject")
            if not rationale.strip():
                raise ValueError("review rationale must not be empty")
            self.review_queue.complete(case_id, reviewer_id)
            record.state = CaseState.DECIDED
            record.decision = decision
            record.version += 1
            self.ledger.append(
                case_id,
                "review_decided",
                reviewer_id,
                {
                    "decision": decision.value,
                    "overrode_agent": decision is not record.case.agent.action,
                    "rationale": rationale,
                    "version": record.version,
                },
            )
            return record

    def get(self, case_id: str) -> CaseRecord:
        with self._lock:
            return self._get(case_id)

    def _get(self, case_id: str) -> CaseRecord:
        try:
            return self._records[case_id]
        except KeyError as exc:
            raise KeyError(f"unknown case {case_id!r}") from exc

    @staticmethod
    def _expect_version(record: CaseRecord, expected_version: int) -> None:
        if record.version != expected_version:
            raise VersionConflict(
                f"expected version {expected_version}, current version is {record.version}"
            )
