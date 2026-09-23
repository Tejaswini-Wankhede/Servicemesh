"""Idempotency management.

The rule ServiceMesh enforces
-----------------------------
A mutating operation's idempotency key is a deterministic function of *what the
operation is*, not of *when it was attempted*. Retry attempt 1 and retry
attempt 4 of the same logical reservation carry the identical key, so the
supplier can recognise the replay and return the original reservation rather
than consuming stock twice.

    key = sha256(transaction_id | operation_type | provider_id | business_scope)

`business_scope` is what makes two *legitimately different* operations on the
same transaction distinct - reserving a battery and later reserving a screen
must not collide. It is derived from the operation payload, not from a counter.

Two layers of protection
------------------------
1. ServiceMesh side: `provider_operations.idempotency_key` carries a unique
   index. Before issuing a call we look for an existing operation with the same
   key; if it already SUCCEEDED we reuse its recorded response and never touch
   the network.
2. Organization side: the supplier and service centre each enforce their own
   unique index on the key they receive.

Layer 1 alone is insufficient (ServiceMesh may crash after sending but before
recording). Layer 2 alone is insufficient (we would still burn a network call
and have to interpret the replay). Both together make retries genuinely safe.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from servicemesh.core.enums import MUTATING_OPERATIONS, OperationStatus, OperationType
from servicemesh.core.models import ProviderOperation


def business_scope(operation: OperationType, payload: dict) -> str:
    """The payload fields that make this operation logically distinct."""
    match operation:
        case OperationType.RESERVE_PART | OperationType.RELEASE_PART:
            return json.dumps(
                {"sku": payload.get("sku"), "quantity": payload.get("quantity", 1)},
                sort_keys=True,
            )
        case OperationType.BOOK_SERVICE | OperationType.CANCEL_SERVICE:
            return json.dumps(
                {
                    "serial_number": payload.get("serial_number"),
                    "issue_type": payload.get("issue_type"),
                },
                sort_keys=True,
            )
        case OperationType.UPDATE_REPAIR:
            return json.dumps(
                {
                    "booking_ref": payload.get("booking_ref"),
                    "status": payload.get("status"),
                },
                sort_keys=True,
            )
        case OperationType.CONFIRM_COMPLETION:
            return json.dumps({"booking_ref": payload.get("booking_ref")}, sort_keys=True)
    return json.dumps(payload, sort_keys=True, default=str)


def make_idempotency_key(
    transaction_id: str,
    operation: OperationType,
    provider_id: str | None,
    payload: dict,
) -> str:
    raw = "|".join([
        transaction_id,
        operation.value,
        provider_id or "-",
        business_scope(operation, payload),
    ])
    digest = hashlib.sha256(raw.encode()).hexdigest()[:40]
    # Readable prefix so the key is greppable in provider logs during a demo.
    return f"sm-{operation.value.lower().replace('_', '-')}-{digest}"


def requires_idempotency_key(operation: OperationType) -> bool:
    return operation in MUTATING_OPERATIONS


@dataclass
class IdempotencyLookup:
    """Result of checking whether this operation has already been performed."""

    #: Internal fingerprint. Identifies the *logical* operation and is stored on
    #: the ProviderOperation row so retries accumulate onto one record.
    key: str | None
    #: The key actually transmitted to the organization. Only mutating
    #: operations get one - sending Idempotency-Key on a GET is meaningless.
    wire_key: str | None
    existing: ProviderOperation | None
    reusable: bool

    @property
    def is_replay(self) -> bool:
        return self.reusable and self.existing is not None


def check_idempotency(
    db: Session,
    transaction_id: str,
    operation: OperationType,
    provider_id: str | None,
    payload: dict,
) -> IdempotencyLookup:
    """Decide whether to issue a call, resume one, or reuse a past result.

    A fingerprint is computed for *every* operation, read-only included. That
    is not about idempotency on the wire - a GET needs no protection - it is
    about retry accounting. Without a stable identity, each retry of a
    read-only step would create a new row with attempt_count=1 and the retry
    budget would never exhaust, producing an infinite retry loop against a
    provider that is simply down. The fingerprint makes "this is the same
    logical operation, on its Nth attempt" representable.
    """
    key = make_idempotency_key(transaction_id, operation, provider_id, payload)
    mutating = requires_idempotency_key(operation)

    existing = db.scalar(
        select(ProviderOperation).where(ProviderOperation.idempotency_key == key)
    )
    if existing is None:
        return IdempotencyLookup(
            key=key, wire_key=key if mutating else None, existing=None, reusable=False
        )

    # Only a completed *mutating* success can be reused outright. Re-running a
    # read-only check is harmless and may return fresher data, so it is
    # re-issued; an operation still PENDING/UNKNOWN must be re-driven (and the
    # organization's own idempotency will collapse the duplicate) rather than
    # silently assumed good.
    reusable = mutating and existing.status == OperationStatus.SUCCEEDED.value
    return IdempotencyLookup(
        key=key, wire_key=key if mutating else None,
        existing=existing, reusable=reusable,
    )
