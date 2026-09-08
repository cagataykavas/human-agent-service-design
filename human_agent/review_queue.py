from __future__ import annotations

import heapq
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta


@dataclass(order=True, slots=True)
class QueueItem:
    sort_key: tuple[int, datetime, int]
    case_id: str = field(compare=False)
    priority: int = field(compare=False)
    enqueued_at: datetime = field(compare=False)
    deadline: datetime = field(compare=False)
    reason_codes: tuple[str, ...] = field(compare=False)
    leased_by: str | None = field(default=None, compare=False)
    lease_expires_at: datetime | None = field(default=None, compare=False)


class ReviewQueue:
    """Priority queue with SLA deadlines and expiring reviewer leases."""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._heap: list[QueueItem] = []
        self._items: dict[str, QueueItem] = {}
        self._sequence = 0

    def enqueue(
        self,
        case_id: str,
        priority: int,
        reason_codes: tuple[str, ...],
        *,
        sla: timedelta = timedelta(hours=4),
    ) -> QueueItem:
        if case_id in self._items:
            raise ValueError(f"case {case_id!r} is already queued")
        if not 0 <= priority <= 100:
            raise ValueError("priority must be in [0, 100]")
        now = self._clock()
        self._sequence += 1
        item = QueueItem(
            sort_key=(-priority, now + sla, self._sequence),
            case_id=case_id,
            priority=priority,
            enqueued_at=now,
            deadline=now + sla,
            reason_codes=reason_codes,
        )
        self._items[case_id] = item
        heapq.heappush(self._heap, item)
        return item

    def lease(
        self,
        reviewer_id: str,
        *,
        duration: timedelta = timedelta(minutes=15),
    ) -> QueueItem | None:
        if not reviewer_id:
            raise ValueError("reviewer_id must not be empty")
        now = self._clock()
        deferred: list[QueueItem] = []
        selected: QueueItem | None = None
        while self._heap:
            item = heapq.heappop(self._heap)
            if item.case_id not in self._items:
                continue
            lease_available = (
                item.leased_by is None
                or item.lease_expires_at is None
                or item.lease_expires_at <= now
            )
            if lease_available:
                item.leased_by = reviewer_id
                item.lease_expires_at = now + duration
                selected = item
                deferred.append(item)
                break
            deferred.append(item)
        for item in deferred:
            heapq.heappush(self._heap, item)
        return selected

    def complete(self, case_id: str, reviewer_id: str) -> QueueItem:
        item = self._items.get(case_id)
        if item is None:
            raise KeyError(case_id)
        if item.leased_by != reviewer_id:
            raise PermissionError("only the active lease holder can complete a review")
        del self._items[case_id]
        return item

    def __len__(self) -> int:
        return len(self._items)
