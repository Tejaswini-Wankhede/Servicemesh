"""Domain event bus.

Persist-then-publish (transactional outbox)
-------------------------------------------
Every event is written to `event_records` inside the same database transaction
that changed the Service Transaction, and only then handed to subscribers and
(optionally) to Kafka/Redpanda. Two consequences that matter:

  * the audit trail does not depend on a broker being up, and
  * an event can never describe a state change that was rolled back.

Backend choice
--------------
`memory` is the default and is fully functional: events are persisted and
dispatched synchronously to in-process subscribers. `kafka` additionally
publishes to a broker. The core system does not require Kafka - it is an
integration surface for *other* systems (a provider's own stack, an analytics
pipeline), not an internal message queue ServiceMesh depends on for
correctness. Making it optional is deliberate: a broker outage must not stop
after-sales transactions from progressing.

Consumer idempotency
--------------------
Every subscriber runs through `ProcessedEvent`, a (event_id, consumer) unique
index. A redelivered event is recorded once and skipped thereafter, so
at-least-once delivery does not become at-least-once side effects.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from servicemesh.core.config import get_settings
from servicemesh.core.enums import EventType
from servicemesh.core.models import EventRecord, ProcessedEvent, new_uuid

logger = logging.getLogger("servicemesh.events")


@dataclass
class DomainEvent:
    """Structured event schema shared by every producer and consumer."""

    event_type: EventType
    transaction_id: str | None
    correlation_id: str | None
    source_service: str = "servicemesh-core"
    payload: dict = field(default_factory=dict)
    event_id: str = field(default_factory=new_uuid)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "transaction_id": self.transaction_id,
            "correlation_id": self.correlation_id,
            "source_service": self.source_service,
            "occurred_at": self.occurred_at.isoformat(),
            "payload": self.payload,
        }


Subscriber = Callable[[DomainEvent, Session], None]


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[EventType, list[tuple[str, Subscriber]]] = {}
        self._wildcard: list[tuple[str, Subscriber]] = []
        self._kafka_producer = None
        self._kafka_failed = False

    # ------------------------------------------------------------ wiring
    def subscribe(
        self, event_type: EventType | None, consumer_name: str, handler: Subscriber
    ) -> None:
        if event_type is None:
            self._wildcard.append((consumer_name, handler))
        else:
            self._subscribers.setdefault(event_type, []).append((consumer_name, handler))

    def clear(self) -> None:
        self._subscribers.clear()
        self._wildcard.clear()

    def subscriber_names(self) -> list[str]:
        names = {n for subs in self._subscribers.values() for n, _ in subs}
        names |= {n for n, _ in self._wildcard}
        return sorted(names)

    # ---------------------------------------------------------- publish
    def publish(self, db: Session, event: DomainEvent) -> EventRecord:
        """Persist the event, then dispatch it. Caller owns the commit."""
        record = EventRecord(
            event_id=event.event_id,
            transaction_id=event.transaction_id,
            correlation_id=event.correlation_id,
            event_type=event.event_type.value,
            source_service=event.source_service,
            payload=event.payload,
            published=False,
        )
        db.add(record)
        db.flush()

        delivered = self._dispatch(db, event)
        record.consumed_count = delivered
        record.published = self._publish_external(event)
        return record

    def _dispatch(self, db: Session, event: DomainEvent) -> int:
        handlers = list(self._subscribers.get(event.event_type, [])) + list(self._wildcard)
        delivered = 0
        for consumer_name, handler in handlers:
            if not self._claim(db, event.event_id, consumer_name):
                logger.debug(
                    "event %s already processed by %s; skipping",
                    event.event_id, consumer_name,
                )
                continue
            try:
                handler(event, db)
                delivered += 1
            except Exception:  # noqa: BLE001
                # A failing consumer must not roll back the state change that
                # produced the event. The event stays persisted and can be
                # replayed from event_records.
                logger.exception(
                    "consumer %s failed handling %s", consumer_name, event.event_type
                )
        return delivered

    @staticmethod
    def _claim(db: Session, event_id: str, consumer: str) -> bool:
        """Reserve (event_id, consumer). False means already handled."""
        try:
            with db.begin_nested():
                db.add(ProcessedEvent(event_id=event_id, consumer=consumer))
                db.flush()
            return True
        except IntegrityError:
            return False

    # ------------------------------------------------------------ kafka
    def _publish_external(self, event: DomainEvent) -> bool:
        settings = get_settings()
        if settings.event_backend != "kafka" or self._kafka_failed:
            return False
        try:
            producer = self._producer()
            if producer is None:
                return False
            topic = f"{settings.kafka_topic_prefix}.{event.event_type.value}"
            import json

            producer.produce(
                topic,
                key=(event.transaction_id or event.event_id).encode(),
                value=json.dumps(event.to_dict()).encode(),
            )
            producer.poll(0)
            return True
        except Exception:  # noqa: BLE001
            # Broker trouble is logged and remembered, never fatal.
            logger.warning("kafka publish failed; continuing with persisted event only")
            self._kafka_failed = True
            return False

    def _producer(self):
        if self._kafka_producer is None:
            try:
                from confluent_kafka import Producer  # type: ignore
            except ImportError:
                logger.warning("confluent-kafka not installed; kafka backend disabled")
                self._kafka_failed = True
                return None
            self._kafka_producer = Producer(
                {"bootstrap.servers": get_settings().kafka_bootstrap_servers}
            )
        return self._kafka_producer


#: Module-level singleton.
event_bus = EventBus()


# ---------------------------------------------------------------------------
# Built-in consumers
# ---------------------------------------------------------------------------


def notification_consumer(event: DomainEvent, db: Session) -> None:
    """Turn customer-visible milestones into notifications."""
    from servicemesh.core.models import Notification, ServiceTransaction

    interesting = {
        EventType.TRANSACTION_CREATED: ("Request received",
                                        "We have received your service request."),
        EventType.WARRANTY_VERIFIED: ("Warranty confirmed",
                                      "Your warranty contract has been verified."),
        EventType.SERVICE_SCHEDULED: ("Service scheduled",
                                      "Your repair appointment has been booked."),
        EventType.REPAIR_COMPLETED: ("Repair completed",
                                     "The service centre has completed your repair."),
        EventType.TRANSACTION_CLOSED: ("Request closed",
                                       "Your service request is complete."),
        EventType.TRANSACTION_REJECTED: ("Request cannot proceed",
                                         "Your request could not be approved."),
        EventType.TRANSACTION_ESCALATED: ("Request escalated",
                                          "A specialist is reviewing your request."),
    }
    if event.event_type not in interesting or not event.transaction_id:
        return
    txn = db.get(ServiceTransaction, event.transaction_id)
    if txn is None:
        return
    title, body = interesting[event.event_type]
    reason = event.payload.get("reason")
    db.add(Notification(
        transaction_id=txn.id, recipient_type="CUSTOMER", recipient_id=txn.customer_id,
        title=title, body=f"{body} (ref {txn.reference})" + (f" {reason}" if reason else ""),
    ))


def audit_consumer(event: DomainEvent, db: Session) -> None:
    """Mirror every domain event into the security/operational audit log."""
    from servicemesh.core.models import AuditEvent

    db.add(AuditEvent(
        transaction_id=event.transaction_id,
        actor_id=event.payload.get("actor_id"),
        actor_role=event.payload.get("actor_role", "SYSTEM"),
        action=f"EVENT:{event.event_type.value}",
        target=event.transaction_id,
        outcome="RECORDED",
        details={"event_id": event.event_id, "source": event.source_service},
    ))


def register_default_consumers() -> None:
    event_bus.subscribe(None, "audit", audit_consumer)
    for et in (
        EventType.TRANSACTION_CREATED, EventType.WARRANTY_VERIFIED,
        EventType.SERVICE_SCHEDULED, EventType.REPAIR_COMPLETED,
        EventType.TRANSACTION_CLOSED, EventType.TRANSACTION_REJECTED,
        EventType.TRANSACTION_ESCALATED,
    ):
        event_bus.subscribe(et, "notifications", notification_consumer)
