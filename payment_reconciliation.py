"""Server-only operator recovery for held payment registrations.

Not a public browser feature and not a Moneris retry client. dataKey is
never persisted. A later operator cannot reconstruct the original Card
Validation body. Leave held unless the DB already has SUCCEEDED or a
human independently confirmed failure.

Authorization is PAYMENT_RECONCILIATION_ADMIN_SECRET, never the expiry
cron secret. This is an ops control path, not a LodgeOS staff UI.
"""

from __future__ import annotations

import logging
import os
import secrets
import uuid
from typing import Mapping, Optional

from payment_session import (
    BOOKING_RPC_PENDING_V7,
    BookingRpcContractError,
    booking_rpc_contract,
)

logger = logging.getLogger(__name__)

ADMIN_SECRET_ENV = "PAYMENT_RECONCILIATION_ADMIN_SECRET"
CRON_SECRET_ENV = "PAYMENT_EXPIRY_CRON_SECRET"
MIN_ADMIN_SECRET_LENGTH = 32

LIST_RPC_NAME = "list_held_payment_registrations"
FINALIZE_RPC_NAME = "operator_finalize_held_payment"
RELEASE_RPC_NAME = "release_held_payment_after_confirmed_failure"

SAFE_UNAVAILABLE = "Payment reconciliation is unavailable."
SAFE_UNAUTHORIZED = "Unauthorized."
SAFE_FAILED = "Payment reconciliation failed."
SAFE_NOT_FOUND = "Held payment registration was not found."
SAFE_CONFLICT = "This payment registration cannot be changed."
SAFE_INVALID = "Reconciliation request is invalid."

OPERATOR_SOURCE = "reconciliation_admin"

HELD_ITEM_KEYS = (
    "session_id",
    "booking_reference",
    "session_status",
    "session_created_at",
    "processing_started_at",
    "expires_at",
    "held_seconds",
    "current_registration_idempotency_key",
    "credential_id",
    "registration_status",
    "registration_error_category",
    "credential_created_at",
    "credential_updated_at",
)

UNSAFE_OPERATOR_FIELDS = (
    "dataKey",
    "data_key",
    "session_token",
    "session_token_hash",
    "confirmation_token",
    "cancellation_token",
    "cancellation_token_hash",
    "moneris_payment_method_id",
    "moneris_issuer_id",
    "paymentMethodId",
    "issuerId",
    "pan",
    "cvd",
    "guest_email",
    "guest_id",
    "first_name",
    "last_name",
    "phone",
)

REJECTED_OPERATOR_INPUT_KEYS = frozenset(
    {
        "paymentMethodId",
        "issuerId",
        "moneris_payment_method_id",
        "moneris_issuer_id",
        "dataKey",
        "data_key",
        "booking_status",
        "registration_status",
        "canonical_booking_id",
        "reservation_id",
        "booking_id",
    }
)

_RPC_IDENTIFIERS = {
    "payment_session_not_found": (SAFE_NOT_FOUND, 404),
    "payment_session_not_processing": (SAFE_CONFLICT, 409),
    "payment_session_already_finalized": (SAFE_CONFLICT, 409),
    "credential_not_succeeded": (SAFE_CONFLICT, 409),
    "credential_succeeded_not_releasable": (SAFE_CONFLICT, 409),
    "registration_not_held": (SAFE_CONFLICT, 409),
    "stale_registration_attempt": (SAFE_CONFLICT, 409),
    "reservation_not_pending_payment": (SAFE_CONFLICT, 409),
    "invalid_release_reason": (SAFE_INVALID, 400),
}


class PaymentReconciliationError(Exception):
    def __init__(self, message: str, *, status: int):
        super().__init__(message)
        self.user_message = message
        self.status = status


def configured_reconciliation_admin_secret() -> Optional[str]:
    raw = os.getenv(ADMIN_SECRET_ENV)
    if raw is None:
        return None
    secret = str(raw).strip()
    if len(secret) < MIN_ADMIN_SECRET_LENGTH:
        return None
    return secret


def authorize_reconciliation_admin(authorization_header: Optional[str]) -> None:
    expected = configured_reconciliation_admin_secret()
    if expected is None:
        raise PaymentReconciliationError(SAFE_UNAVAILABLE, status=503)
    if not isinstance(authorization_header, str) or not authorization_header.strip():
        raise PaymentReconciliationError(SAFE_UNAUTHORIZED, status=401)
    parts = authorization_header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise PaymentReconciliationError(SAFE_UNAUTHORIZED, status=401)
    if not _secrets_match(parts[1], expected):
        raise PaymentReconciliationError(SAFE_UNAUTHORIZED, status=401)


def authorize_reconciliation_admin_posted_secret(provided) -> None:
    """Authorize a POST-body secret against PAYMENT_RECONCILIATION_ADMIN_SECRET.

    Used only by the temporary HT render-check form. Query strings, cookies,
    and Bearer headers are ignored by the caller.
    """
    expected = configured_reconciliation_admin_secret()
    if expected is None:
        raise PaymentReconciliationError(SAFE_UNAVAILABLE, status=503)
    if not isinstance(provided, str) or provided == "":
        raise PaymentReconciliationError(SAFE_UNAUTHORIZED, status=401)
    if not _secrets_match(provided, expected):
        raise PaymentReconciliationError(SAFE_UNAUTHORIZED, status=401)


def require_pending_v7_for_reconciliation() -> None:
    try:
        contract = booking_rpc_contract()
    except BookingRpcContractError:
        raise PaymentReconciliationError(SAFE_UNAVAILABLE, status=503) from None
    if contract != BOOKING_RPC_PENDING_V7:
        raise PaymentReconciliationError(SAFE_UNAVAILABLE, status=503)


def list_held_payment_registrations(supabase) -> dict:
    payload = _call_rpc(supabase, LIST_RPC_NAME, {})
    held = payload.get("held")
    if payload.get("ok") is not True or not isinstance(held, list):
        raise PaymentReconciliationError(SAFE_FAILED, status=500)
    return {"ok": True, "held": [_public_held_item(item) for item in held]}


def finalize_held_payment(supabase, session_id: str, request_body) -> dict:
    _reject_operator_overrides(request_body)
    payload = _call_rpc(
        supabase,
        FINALIZE_RPC_NAME,
        {"p_session_id": _require_uuid(session_id)},
    )
    if payload.get("ok") is not True:
        raise PaymentReconciliationError(SAFE_FAILED, status=500)
    return {
        "ok": True,
        "idempotent": bool(payload.get("idempotent")),
        "booking_reference": payload.get("booking_reference"),
    }


def release_held_payment_confirmed_failure(
    supabase, session_id: str, request_body
) -> dict:
    _reject_operator_overrides(request_body)
    body = request_body if isinstance(request_body, Mapping) else {}
    attempt_key = _require_uuid(
        body.get("current_registration_idempotency_key"),
        missing_status=400,
    )
    args = {
        "p_session_id": _require_uuid(session_id),
        "p_current_registration_idempotency_key": attempt_key,
    }
    reason = _optional_reason(body.get("reason"))
    if reason is not None:
        args["p_reason"] = reason
    payload = _call_rpc(supabase, RELEASE_RPC_NAME, args)
    if payload.get("ok") is not True:
        raise PaymentReconciliationError(SAFE_FAILED, status=500)
    return {"ok": True, "booking_reference": payload.get("booking_reference")}


def reconciliation_error_body(exc: PaymentReconciliationError) -> dict:
    return {"ok": False, "error": exc.user_message}


def _call_rpc(supabase, name: str, args: dict) -> dict:
    try:
        res = supabase.rpc(name, args).execute()
    except Exception as exc:  # noqa: BLE001
        logger.error("%s failed: %s", name, type(exc).__name__)
        mapped = _map_rpc_exception(exc)
        if mapped is not None:
            raise mapped from None
        raise PaymentReconciliationError(SAFE_FAILED, status=500) from None
    return _rpc_payload(res)


def _map_rpc_exception(exc: Exception) -> Optional[PaymentReconciliationError]:
    message = getattr(exc, "message", None)
    if isinstance(message, str) and message in _RPC_IDENTIFIERS:
        text, status = _RPC_IDENTIFIERS[message]
        return PaymentReconciliationError(text, status=status)
    return None


def _public_held_item(item) -> dict:
    if not isinstance(item, Mapping):
        raise PaymentReconciliationError(SAFE_FAILED, status=500)
    out = {}
    for key in HELD_ITEM_KEYS:
        if key in item:
            out[key] = item[key]
    if "held_seconds" in out:
        seconds = out["held_seconds"]
        if type(seconds) is not int or isinstance(seconds, bool) or seconds < 0:
            raise PaymentReconciliationError(SAFE_FAILED, status=500)
    for key in UNSAFE_OPERATOR_FIELDS:
        out.pop(key, None)
    return out


def _reject_operator_overrides(request_body) -> None:
    if not isinstance(request_body, Mapping):
        return
    if REJECTED_OPERATOR_INPUT_KEYS.intersection(request_body):
        raise PaymentReconciliationError(SAFE_INVALID, status=400)


def _optional_reason(value) -> Optional[str]:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise PaymentReconciliationError(SAFE_INVALID, status=400)
    reason = value.strip()
    if not reason:
        return None
    if len(reason) > 200 or any(ord(ch) < 32 for ch in reason):
        raise PaymentReconciliationError(SAFE_INVALID, status=400)
    return reason


def _require_uuid(value, *, missing_status: int = 404) -> str:
    if value is None or value == "":
        raise PaymentReconciliationError(SAFE_INVALID, status=missing_status)
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise PaymentReconciliationError(SAFE_NOT_FOUND, status=404) from None


def _secrets_match(provided: str, expected: str) -> bool:
    if not isinstance(provided, str) or not isinstance(expected, str):
        return False
    try:
        return secrets.compare_digest(provided, expected)
    except (TypeError, ValueError):
        return False


def _rpc_payload(res):
    data = getattr(res, "data", None)
    if isinstance(data, list):
        return data[0] if data else {}
    return data or {}
