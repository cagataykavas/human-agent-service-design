from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class AuditEvent:
    sequence: int
    case_id: str
    event_type: str
    actor: str
    occurred_at: datetime
    payload: dict[str, object]
    previous_hash: str
    event_hash: str


class AuditLedger:
    """Append-only, hash-chained event ledger for reproducible decision history."""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._events: dict[str, list[AuditEvent]] = {}

    @staticmethod
    def _hash(body: dict[str, object]) -> str:
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def append(
        self,
        case_id: str,
        event_type: str,
        actor: str,
        payload: dict[str, object] | None = None,
    ) -> AuditEvent:
        stream = self._events.setdefault(case_id, [])
        previous_hash = stream[-1].event_hash if stream else "GENESIS"
        occurred_at = self._clock()
        if occurred_at.tzinfo is None:
            raise ValueError("audit clock must return a timezone-aware datetime")
        body: dict[str, object] = {
            "sequence": len(stream) + 1,
            "case_id": case_id,
            "event_type": event_type,
            "actor": actor,
            "occurred_at": occurred_at.astimezone(UTC).isoformat(),
            "payload": payload or {},
            "previous_hash": previous_hash,
        }
        event = AuditEvent(
            sequence=len(stream) + 1,
            case_id=case_id,
            event_type=event_type,
            actor=actor,
            occurred_at=occurred_at,
            payload=payload or {},
            previous_hash=previous_hash,
            event_hash=self._hash(body),
        )
        stream.append(event)
        return event

    def stream(self, case_id: str) -> tuple[AuditEvent, ...]:
        return tuple(self._events.get(case_id, ()))

    def verify(self, case_id: str) -> bool:
        previous_hash = "GENESIS"
        for event in self.stream(case_id):
            body: dict[str, object] = {
                "sequence": event.sequence,
                "case_id": event.case_id,
                "event_type": event.event_type,
                "actor": event.actor,
                "occurred_at": event.occurred_at.astimezone(UTC).isoformat(),
                "payload": event.payload,
                "previous_hash": event.previous_hash,
            }
            if event.previous_hash != previous_hash or self._hash(body) != event.event_hash:
                return False
            previous_hash = event.event_hash
        return True
