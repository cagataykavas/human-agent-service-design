from __future__ import annotations

from dataclasses import dataclass
from threading import Event, Thread
from typing import Any, Protocol

from service_desk.errors import Conflict
from service_desk.models import AgentJob, AgentJobState, IssuePriority
from service_desk.repository import SQLiteServiceDeskRepository
from service_desk.service import ServiceDesk
from service_desk.tools import ToolContext, ToolRegistry


class AgentModel(Protocol):
    def complete(
        self,
        capability: str,
        payload: dict[str, Any],
        *,
        abort_event: Event,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class AgentExecutionResult:
    job: AgentJob
    tool_calls: tuple[str, ...]
    lease_lost: bool = False


class DeterministicAgentModel:
    """Reproducible development model; production models implement the same contract."""

    def complete(
        self,
        capability: str,
        payload: dict[str, Any],
        *,
        abort_event: Event,
    ) -> dict[str, Any]:
        if abort_event.is_set():
            raise RuntimeError("agent execution was aborted before model dispatch")
        text = " ".join(str(value) for value in payload.values()).lower()
        if capability == "triage":
            security_terms = ("breach", "credential", "ransomware", "security")
            outage_terms = ("outage", "unavailable", "production down")
            if any(term in text for term in security_terms):
                return {
                    "category": "security",
                    "priority": IssuePriority.CRITICAL.value,
                    "confidence": 0.93,
                    "reason_codes": ["security_term"],
                }
            if any(term in text for term in outage_terms):
                return {
                    "category": "incident",
                    "priority": IssuePriority.HIGH.value,
                    "confidence": 0.88,
                    "reason_codes": ["availability_term"],
                }
            return {
                "category": "service_request",
                "priority": IssuePriority.MEDIUM.value,
                "confidence": 0.72,
                "reason_codes": ["default_route"],
            }
        if capability == "summarize":
            return {"summary": text[:240], "confidence": 1.0}
        if capability == "recommend_action":
            return {
                "recommendation": "review_knowledge_then_assign",
                "confidence": 0.68,
                "requires_human": True,
            }
        return {"query": text[:200], "confidence": 0.8}


class _LeaseHeartbeat:
    """Renew one fenced attempt and expose lease loss as a cooperative abort."""

    def __init__(
        self,
        repository: SQLiteServiceDeskRepository,
        job: AgentJob,
        *,
        worker_id: str,
        lease_seconds: int,
        interval_seconds: float,
    ) -> None:
        self.repository = repository
        self.job = job
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.interval_seconds = interval_seconds
        self.abort_event = Event()
        self._stop_event = Event()
        self._thread = Thread(
            target=self._run,
            name=f"agent-lease-{job.job_id}",
            daemon=True,
        )

    @property
    def lease_lost(self) -> bool:
        return self.abort_event.is_set()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=min(1.0, self.interval_seconds * 2))

    def _run(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            try:
                self.repository.renew_agent_job_lease(
                    self.job.job_id,
                    self.worker_id,
                    expected_attempt=self.job.attempt,
                    lease_seconds=self.lease_seconds,
                )
            # Failing to prove renewal is lease loss; detailed storage errors stay local.
            except Exception:  # noqa: BLE001
                self.abort_event.set()
                return


class AgentWorker:
    """One-job worker with leases, typed model output and governed tools."""

    def __init__(
        self,
        repository: SQLiteServiceDeskRepository,
        desk: ServiceDesk,
        model: AgentModel,
        tools: ToolRegistry,
        *,
        worker_id: str,
        lease_seconds: int = 60,
        heartbeat_interval_seconds: float = 20.0,
    ) -> None:
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int):
            raise TypeError("lease_seconds must be an integer")
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be in [1, 3600]")
        if isinstance(heartbeat_interval_seconds, bool) or not isinstance(
            heartbeat_interval_seconds, (int, float)
        ):
            raise TypeError("heartbeat_interval_seconds must be numeric")
        if not 0 < heartbeat_interval_seconds <= lease_seconds / 2:
            raise ValueError(
                "heartbeat_interval_seconds must be positive and at most half the lease"
            )
        self.repository = repository
        self.desk = desk
        self.model = model
        self.tools = tools
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.heartbeat_interval_seconds = float(heartbeat_interval_seconds)

    def run_once(self) -> AgentExecutionResult | None:
        job = self.repository.claim_agent_job(self.worker_id, lease_seconds=self.lease_seconds)
        if job is None:
            return None
        requested_calls: list[str] = []
        heartbeat = _LeaseHeartbeat(
            self.repository,
            job,
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
            interval_seconds=self.heartbeat_interval_seconds,
        )
        heartbeat.start()
        try:
            output = self.model.complete(
                job.capability,
                job.input_payload,
                abort_event=heartbeat.abort_event,
            )
            if heartbeat.lease_lost:
                return AgentExecutionResult(job, tuple(requested_calls), lease_lost=True)
            if job.capability == "retrieve_knowledge":
                call = self.desk.request_tool_call(
                    job_id=job.job_id,
                    job_attempt=job.attempt,
                    tool_name="knowledge_search",
                    arguments={"query": output.get("query", "")},
                    requested_by=self.worker_id,
                )
                if heartbeat.lease_lost:
                    return AgentExecutionResult(job, tuple(requested_calls), lease_lost=True)
                tool_result = self.tools.execute(
                    call.tool_name,
                    call.arguments,
                    ToolContext(self.worker_id, job.job_id),
                )
                if heartbeat.lease_lost:
                    return AgentExecutionResult(job, tuple(requested_calls), lease_lost=True)
                self.repository.complete_tool_call(call.call_id, tool_result)
                output["evidence"] = tool_result["results"]
                requested_calls.append(call.call_id)
            heartbeat.stop()
            if heartbeat.lease_lost:
                return AgentExecutionResult(job, tuple(requested_calls), lease_lost=True)
            finished = self.repository.finish_agent_job(
                job.job_id,
                self.worker_id,
                AgentJobState.SUCCEEDED,
                expected_attempt=job.attempt,
                output=output,
            )
        # The worker is a process boundary: model/tool failures become durable job state.
        except Exception as exc:  # noqa: BLE001
            if heartbeat.lease_lost:
                return AgentExecutionResult(job, tuple(requested_calls), lease_lost=True)
            heartbeat.stop()
            if heartbeat.lease_lost:
                return AgentExecutionResult(job, tuple(requested_calls), lease_lost=True)
            try:
                finished = self.repository.finish_agent_job(
                    job.job_id,
                    self.worker_id,
                    AgentJobState.FAILED,
                    expected_attempt=job.attempt,
                    error=f"{type(exc).__name__}: {exc}",
                )
            except Conflict:
                return AgentExecutionResult(job, tuple(requested_calls), lease_lost=True)
        finally:
            heartbeat.stop()
        return AgentExecutionResult(finished, tuple(requested_calls))
