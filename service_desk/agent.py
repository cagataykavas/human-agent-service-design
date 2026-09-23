from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from service_desk.models import AgentJob, AgentJobState, IssuePriority
from service_desk.repository import SQLiteServiceDeskRepository
from service_desk.service import ServiceDesk
from service_desk.tools import ToolContext, ToolRegistry


class AgentModel(Protocol):
    def complete(self, capability: str, payload: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class AgentExecutionResult:
    job: AgentJob
    tool_calls: tuple[str, ...]


class DeterministicAgentModel:
    """Reproducible development model; production models implement the same contract."""

    def complete(self, capability: str, payload: dict[str, Any]) -> dict[str, Any]:
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
    ) -> None:
        self.repository = repository
        self.desk = desk
        self.model = model
        self.tools = tools
        self.worker_id = worker_id

    def run_once(self) -> AgentExecutionResult | None:
        job = self.repository.claim_agent_job(self.worker_id)
        if job is None:
            return None
        requested_calls: list[str] = []
        try:
            output = self.model.complete(job.capability, job.input_payload)
            if job.capability == "retrieve_knowledge":
                call = self.desk.request_tool_call(
                    job_id=job.job_id,
                    tool_name="knowledge_search",
                    arguments={"query": output.get("query", "")},
                    requested_by=self.worker_id,
                )
                self.repository.start_tool_call(
                    call.call_id,
                    self.worker_id,
                    call.request_digest or "",
                )
                tool_result = self.tools.execute(
                    call.tool_name,
                    call.arguments,
                    ToolContext(self.worker_id, job.job_id),
                )
                self.repository.complete_tool_call(
                    call.call_id,
                    tool_result,
                    execution_owner=self.worker_id,
                )
                output["evidence"] = tool_result["results"]
                requested_calls.append(call.call_id)
            finished = self.repository.finish_agent_job(
                job.job_id,
                self.worker_id,
                AgentJobState.SUCCEEDED,
                output=output,
            )
        # The worker is a process boundary: model/tool failures become durable job state.
        except Exception as exc:  # noqa: BLE001
            finished = self.repository.finish_agent_job(
                job.job_id,
                self.worker_id,
                AgentJobState.FAILED,
                error=f"{type(exc).__name__}: {exc}",
            )
        return AgentExecutionResult(finished, tuple(requested_calls))
