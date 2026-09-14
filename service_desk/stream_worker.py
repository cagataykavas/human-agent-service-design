"""Run the outbox publisher or idempotent Kafka consumer with explicit configuration."""

from __future__ import annotations

import argparse
import os
import socket
import time

from service_desk.repository import SQLiteServiceDeskRepository
from service_desk.streaming import (
    IssueEventProjection,
    KafkaPublisher,
    OutboxDispatcher,
    consume_one,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Kafka-backed service desk event worker")
    parser.add_argument("mode", choices=("publish", "consume"))
    parser.add_argument(
        "--database", default=os.getenv("SERVICE_DESK_DATABASE_PATH", "service-desk.db")
    )
    parser.add_argument(
        "--bootstrap", default=os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    )
    parser.add_argument("--topic", default="service-desk.events.v1")
    parser.add_argument("--once", action="store_true", help="Run one polling iteration")
    args = parser.parse_args()
    repository = SQLiteServiceDeskRepository(args.database)
    if args.mode == "publish":
        dispatcher = OutboxDispatcher(
            repository,
            KafkaPublisher(args.bootstrap, args.topic),
            owner=f"publisher-{socket.gethostname()}-{os.getpid()}",
        )
        while True:
            delivered, failed = dispatcher.drain()
            print(f"delivered={delivered} failed={failed}", flush=True)
            if args.once:
                return
            time.sleep(1)
    else:
        from confluent_kafka import Consumer

        consumer = Consumer(
            {
                "bootstrap.servers": args.bootstrap,
                "group.id": "service-desk-projection-v1",
                "auto.offset.reset": "earliest",
                "enable.auto.commit": False,
            }
        )
        consumer.subscribe([args.topic])
        projection = IssueEventProjection(repository)
        try:
            while True:
                message = consumer.poll(1)
                if message is not None:
                    print(f"applied={consume_one(projection, message, consumer)}", flush=True)
                if args.once:
                    return
        finally:
            consumer.close()


if __name__ == "__main__":
    main()
