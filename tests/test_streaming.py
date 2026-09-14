from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from service_desk.models import Actor, ActorRole, IssuePriority
from service_desk.repository import SQLiteServiceDeskRepository
from service_desk.service import ServiceDesk
from service_desk.streaming import (
    EventEnvelope,
    IssueEventProjection,
    OutboxDispatcher,
    consume_one,
)


class Clock:
    def __init__(self) -> None:
        self.current = datetime(2026, 9, 14, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current


class Publisher:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bytes]] = []
        self.fail = False

    def publish(self, *, key: str, value: bytes) -> None:
        if self.fail:
            raise ConnectionError("broker unavailable")
        self.messages.append((key, value))


def setup(tmp_path: Path) -> tuple[SQLiteServiceDeskRepository, str, Clock]:
    clock = Clock()
    repo = SQLiteServiceDeskRepository(tmp_path / "stream.db", clock=clock)
    desk = ServiceDesk(repo)
    desk.bootstrap()
    project = desk.create_project(key="OPS", name="Operations")
    issue = desk.create_issue(
        project_id=project.project_id,
        summary="VPN access",
        description="Access is blocked",
        priority=IssuePriority.HIGH,
        reporter=Actor("customer", ActorRole.REQUESTER),
        idempotency_key="req-1",
    )
    return repo, issue.issue_id, clock


def test_end_to_end_outbox_publish_and_deduplicated_projection(tmp_path: Path) -> None:
    repo, issue_id, _ = setup(tmp_path)
    publisher = Publisher()
    dispatcher = OutboxDispatcher(repo, publisher, owner="worker-1")
    assert dispatcher.drain() == (1, 0)
    assert dispatcher.drain() == (0, 0)
    assert publisher.messages[0][0] == issue_id
    projection = IssueEventProjection(repo)
    raw = publisher.messages[0][1]
    assert projection.apply(raw) is True
    assert projection.apply(raw) is False
    assert projection.counts(issue_id) == {"issue.created": 1}


def test_broker_failure_retries_after_delay_and_does_not_lose_event(tmp_path: Path) -> None:
    repo, issue_id, clock = setup(tmp_path)
    publisher = Publisher()
    publisher.fail = True
    dispatcher = OutboxDispatcher(repo, publisher, owner="worker-1", retry_delay_seconds=20)
    assert dispatcher.drain() == (0, 1)
    publisher.fail = False
    assert dispatcher.drain() == (0, 0)
    clock.current += timedelta(seconds=21)
    assert dispatcher.drain() == (1, 0)
    assert publisher.messages[0][0] == issue_id
    assert EventEnvelope.decode(publisher.messages[0][1]).event_type == "issue.created"


def test_publish_ack_failure_replays_but_inbox_remains_exactly_once(tmp_path: Path) -> None:
    repo, issue_id, clock = setup(tmp_path)
    publisher = Publisher()
    dispatcher = OutboxDispatcher(repo, publisher, owner="worker-1")
    original_ack = repo.acknowledge_outbox

    def failed_ack(_event_id: str, _owner: str) -> None:
        raise ConnectionError("process died after broker acknowledgement")

    repo.acknowledge_outbox = failed_ack  # type: ignore[method-assign]
    with pytest.raises(ConnectionError):
        dispatcher.drain()
    repo.acknowledge_outbox = original_ack  # type: ignore[method-assign]
    clock.current += timedelta(seconds=31)
    assert dispatcher.drain() == (1, 0)
    assert len(publisher.messages) == 2
    projection = IssueEventProjection(repo)
    assert [projection.apply(raw) for _, raw in publisher.messages] == [True, False]
    assert projection.counts(issue_id)["issue.created"] == 1


def test_invalid_schema_does_not_enter_inbox(tmp_path: Path) -> None:
    repo, issue_id, _ = setup(tmp_path)
    projection = IssueEventProjection(repo)
    raw = EventEnvelope("e", issue_id, "issue.created", {}, "2026-09-14T00:00:00+00:00")
    with pytest.raises(ValueError, match="schema"):
        projection.apply(raw.encode().replace(b'"schema_version":1', b'"schema_version":2'))
    assert projection.counts(issue_id) == {}


def test_consumer_commits_offset_only_after_persisting(tmp_path: Path) -> None:
    repo, issue_id, _ = setup(tmp_path)
    projection = IssueEventProjection(repo)
    raw = EventEnvelope("e", issue_id, "issue.created", {}, "2026-09-14T00:00:00+00:00").encode()

    class Message:
        def error(self):
            return None

        def value(self):
            return raw

    class Consumer:
        commits = 0

        def commit(self, *, message, asynchronous):
            assert message is not None and asynchronous is False
            self.commits += 1

    consumer = Consumer()
    assert consume_one(projection, Message(), consumer) is True
    assert consume_one(projection, Message(), consumer) is False
    assert consumer.commits == 2
    assert projection.counts(issue_id)["issue.created"] == 1
