"""At-least-once Kafka delivery with idempotent, transactionally updated projections."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from service_desk.models import OutboxEvent
from service_desk.repository import SQLiteServiceDeskRepository


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    event_id: str
    aggregate_id: str
    event_type: str
    payload: dict[str, Any]
    occurred_at: str
    schema_version: int = 1

    def encode(self) -> bytes:
        return json.dumps(
            {
                "schema_version": self.schema_version,
                "event_id": self.event_id,
                "aggregate_id": self.aggregate_id,
                "event_type": self.event_type,
                "payload": self.payload,
                "occurred_at": self.occurred_at,
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")

    @classmethod
    def decode(cls, raw: bytes) -> EventEnvelope:
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("schema_version") != 1:
            raise ValueError("unsupported event schema version")
        required = {"event_id", "aggregate_id", "event_type", "occurred_at"}
        if not all(isinstance(data.get(key), str) and data[key] for key in required):
            raise ValueError("event envelope has missing identifiers")
        if not isinstance(data.get("payload"), dict):
            raise TypeError("event payload must be an object")
        datetime.fromisoformat(data["occurred_at"])
        return cls(**{key: data[key] for key in (*required, "payload")})


class EventPublisher(Protocol):
    def publish(self, *, key: str, value: bytes) -> None: ...


class KafkaPublisher:
    """Wait for broker acknowledgement before the outbox row can be marked delivered."""

    def __init__(self, bootstrap_servers: str, topic: str = "service-desk.events.v1") -> None:
        from confluent_kafka import Producer

        self.topic = topic
        self.producer = Producer(
            {
                "bootstrap.servers": bootstrap_servers,
                "acks": "all",
                "enable.idempotence": True,
                "message.timeout.ms": 10_000,
            }
        )

    def publish(self, *, key: str, value: bytes) -> None:
        delivery: list[Exception | None] = []

        def callback(error: Any, _message: Any) -> None:
            delivery.append(RuntimeError(str(error)) if error else None)

        try:
            self.producer.produce(self.topic, key=key.encode(), value=value, on_delivery=callback)
        except Exception as exc:
            raise RuntimeError("Kafka produce failed") from exc
        remaining = self.producer.flush(12)
        if remaining or not delivery:
            raise TimeoutError("broker did not acknowledge the event")
        if delivery[0] is not None:
            raise delivery[0]


class OutboxDispatcher:
    def __init__(
        self,
        repository: SQLiteServiceDeskRepository,
        publisher: EventPublisher,
        *,
        owner: str,
        retry_delay_seconds: int = 30,
    ) -> None:
        if not owner or retry_delay_seconds < 1:
            raise ValueError("owner and positive retry delay are required")
        self.repository = repository
        self.publisher = publisher
        self.owner = owner
        self.retry_delay_seconds = retry_delay_seconds

    def drain(self, limit: int = 100) -> tuple[int, int]:
        delivered = failed = 0
        for event in self.repository.pending_outbox(self.owner, limit):
            envelope = self._envelope(event)
            try:
                self.publisher.publish(key=event.aggregate_id, value=envelope.encode())
            except (OSError, RuntimeError, TimeoutError, BufferError) as exc:
                self.repository.reject_outbox(
                    event.event_id, self.owner, str(exc), self.retry_delay_seconds
                )
                failed += 1
            else:
                self.repository.acknowledge_outbox(event.event_id, self.owner)
                delivered += 1
        return delivered, failed

    def _envelope(self, event: OutboxEvent) -> EventEnvelope:
        return EventEnvelope(
            event_id=event.event_id,
            aggregate_id=event.aggregate_id,
            event_type=event.event_type,
            payload=event.payload,
            occurred_at=event.available_at.astimezone(UTC).isoformat(),
        )


class IssueEventProjection:
    """Inbox deduplication and materialized counters commit in one SQLite transaction."""

    def __init__(self, repository: SQLiteServiceDeskRepository) -> None:
        self.repository = repository
        with repository.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS event_inbox (
                    event_id TEXT PRIMARY KEY, consumed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS issue_event_counts (
                    aggregate_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_count INTEGER NOT NULL,
                    PRIMARY KEY(aggregate_id, event_type)
                );
                """
            )

    def apply(self, raw: bytes) -> bool:
        event = EventEnvelope.decode(raw)
        if not event.event_type.startswith("issue."):
            raise ValueError("only issue events are accepted")
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO event_inbox(event_id, consumed_at) VALUES (?, ?)",
                    (event.event_id, self.repository.time(self.repository.now())),
                )
            except sqlite3.IntegrityError:
                return False
            connection.execute(
                """
                INSERT INTO issue_event_counts(aggregate_id, event_type, event_count)
                VALUES (?, ?, 1)
                ON CONFLICT(aggregate_id, event_type)
                DO UPDATE SET event_count = event_count + 1
                """,
                (event.aggregate_id, event.event_type),
            )
            connection.commit()
        return True

    def counts(self, aggregate_id: str) -> dict[str, int]:
        with self.repository.connect() as connection:
            rows = connection.execute(
                "SELECT event_type, event_count FROM issue_event_counts WHERE aggregate_id = ?",
                (aggregate_id,),
            ).fetchall()
        return {row["event_type"]: row["event_count"] for row in rows}


def consume_one(projection: IssueEventProjection, message: Any, consumer: Any) -> bool:
    """Commit Kafka offset only after the durable projection transaction succeeds."""
    if message.error():
        raise RuntimeError(str(message.error()))
    applied = projection.apply(message.value())
    consumer.commit(message=message, asynchronous=False)
    return applied
