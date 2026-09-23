"""API dependencies: who is calling, and what are they allowed to touch.

Authorization is enforced here, on the server, for every request. The frontend
hides what a user cannot use, but hiding is presentation, not security - a
CUSTOMER who crafts a request for another customer's transaction gets a 404
from this layer regardless of what the UI showed them.

Why 404 and not 403 for cross-tenant access
-------------------------------------------
Returning 403 for a transaction that exists and 404 for one that does not lets
an attacker enumerate valid transaction ids. Both cases return 404.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from servicemesh.core.db import get_db
from servicemesh.core.enums import UserRole
from servicemesh.core.models import AuditEvent, ServiceTransaction, User
from servicemesh.core.security import TokenError, decode_access_token

bearer_scheme = HTTPBearer(auto_error=False)

DbSession = Annotated[Session, Depends(get_db)]


def get_current_user(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ] = None,
    db: Session = Depends(get_db),
) -> User:
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "not_authenticated", "message": "missing bearer token"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = decode_access_token(credentials.credentials)
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid_token", "message": str(exc)},
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    user = db.get(User, payload.get("sub", ""))
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid_token", "message": "user not found or inactive"},
        )
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_roles(*roles: UserRole) -> Callable[..., User]:
    allowed = {r.value for r in roles}

    def _dependency(user: CurrentUser) -> User:
        if user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "forbidden",
                    "message": (
                        f"role {user.role} may not perform this action; "
                        f"requires one of {sorted(allowed)}"
                    ),
                },
            )
        return user

    return _dependency


require_admin = require_roles(UserRole.ADMIN)
require_provider = require_roles(UserRole.PROVIDER)
require_repair_person = require_roles(UserRole.REPAIR_PERSON)
require_customer = require_roles(UserRole.CUSTOMER)
require_provider_or_admin = require_roles(UserRole.PROVIDER, UserRole.ADMIN)
require_repair_workflow = require_roles(
    UserRole.PROVIDER, UserRole.REPAIR_PERSON, UserRole.ADMIN
)


def get_owned_transaction(
    transaction_id: str, user: User, db: Session
) -> ServiceTransaction:
    """Fetch a transaction the caller is entitled to see.

    CUSTOMER  -> only their own transactions
    PROVIDER  -> only transactions their organization participates in
    ADMIN     -> everything
    """
    txn = db.get(ServiceTransaction, transaction_id)
    if txn is None:
        raise _not_found()

    if user.role == UserRole.ADMIN.value:
        return txn

    if user.role == UserRole.CUSTOMER.value:
        if txn.customer_id != user.customer_id:
            raise _not_found()
        return txn

    if user.role == UserRole.PROVIDER.value:
        if user.provider_id is None:
            raise _not_found()

        participating = {p.provider_id for p in txn.participants}

        is_participant = user.provider_id in participating
        is_selected_service_centre = (
           txn.selected_service_provider_id == user.provider_id
    )
        is_selected_supplier = (
           txn.selected_supplier_id == user.provider_id
    )

        if not (
           is_participant
           or is_selected_service_centre
           or is_selected_supplier
    ):
            raise _not_found()

        return txn

    if user.role == UserRole.REPAIR_PERSON.value:
        if (
            user.provider_id is None
            or txn.selected_service_provider_id != user.provider_id
        ):
            raise _not_found()
        return txn

    raise _not_found()


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "not_found", "message": "transaction not found"},
    )


def audit(
    db: Session,
    user: User | None,
    action: str,
    *,
    target: str | None = None,
    outcome: str = "SUCCESS",
    details: dict | None = None,
    transaction_id: str | None = None,
) -> None:
    db.add(AuditEvent(
        transaction_id=transaction_id,
        actor_id=user.id if user else None,
        actor_role=user.role if user else None,
        action=action, target=target, outcome=outcome,
        details=details or {},
    ))
