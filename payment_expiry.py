"""Protected caller for expire_abandoned_payment_sessions().

This is not a scheduler. An external process POSTs
``/api/internal/expire-payment-sessions`` with a dedicated bearer secret.
PostgreSQL remains the state-machine authority (SKIP LOCKED). Flask must
not cancel rows, inspect cards, call Moneris, or retry this RPC.

Do not use an in-process Flask timer. Vercel serverless instances do not
provide a reliable background loop.

Recommended external cadence: every 5 minutes. Do not use a once-per-day
caller for this hold. Do not change SESSION_TTL (20 minutes) or
PROCESSING_TIMEOUT (10 minutes).

PAYMENT_EXPIRY_CRON_SECRET is scheduler-agnostic. Do not reuse
MONERIS_CLIENT_SECRET, the Supabase service-role key, or guest tokens.
If Vercel Cron is later approved, keep this env name and point Vercel at
the same bearer value (do not switch the design to CRON_SECRET unless
explicitly chosen).
"""

from __future__ import annotations

import logging
import os
import secrets
from typing import Mapping, Optional

from payment_session import (
    BOOKING_RPC_PENDING_V7,
    BookingRpcContractError,
    booking_rpc_contract,
    leaked_internal_keys,
)

logger = logging.getLogger(__name__)

CRON_SECRET_ENV = "PAYMENT_EXPIRY_CRON_SECRET"
EXPIRE_RPC_NAME = "expire_abandoned_payment_sessions"
MIN_CRON_SECRET_LENGTH = 32
RECOMMENDED_CADENCE_MINUTES = 5

SAFE_COUNTER_KEYS = (
    "expired",
    "inconsistent",
    "held_pending",
    "held_reconciliation",
)
SAFE_RESPONSE_KEYS = frozenset(("ok", "error") + SAFE_COUNTER_KEYS)

SAFE_UNAVAILABLE = "Payment session expiry is unavailable."
SAFE_UNAUTHORIZED = "Unauthorized."
SAFE_FAILED = "Payment session expiry failed."

# Caller-supplied JSON/query keys that must never reach the RPC.
REJECTED_CLIENT_ID_KEYS = frozenset(
    {
        "session_id",
        "reservation_id",
        "canonical_booking_id",
        "booking_id",
        "booking_reference",
        "current_registration_idempotency_key",
        "registration_idempotency_key",
        "credential_id",
        "payment_session_token",
        "dataKey",
        "data_key",
    }
)


class PaymentExpiryError(Exception):
    """Fail-closed expiry caller with a scheduler-safe message."""

    def __init__(self, message: str, *, status: int):
        super().__init__(message)
        self.user_message = message
        self.status = status


def configured_expiry_cron_secret() -> Optional[str]:
    raw = os.getenv(CRON_SECRET_ENV)
    if raw is None:
        return None
    secret = str(raw).strip()
    if len(secret) < MIN_CRON_SECRET_LENGTH:
        return None
    return secret


def authorize_expiry_cron(authorization_header: Optional[str]) -> None:
    """Require ``Authorization: Bearer <PAYMENT_EXPIRY_CRON_SECRET>``."""
    expected = configured_expiry_cron_secret()
    if expected is None:
        raise PaymentExpiryError(SAFE_UNAVAILABLE, status=503)
    if not isinstance(authorization_header, str) or not authorization_header.strip():
        raise PaymentExpiryError(SAFE_UNAUTHORIZED, status=401)
    parts = authorization_header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise PaymentExpiryError(SAFE_UNAUTHORIZED, status=401)
    if not _secrets_match(parts[1], expected):
        raise PaymentExpiryError(SAFE_UNAUTHORIZED, status=401)


def require_pending_v7_for_expiry() -> None:
    try:
        contract = booking_rpc_contract()
    except BookingRpcContractError:
        raise PaymentExpiryError(SAFE_UNAVAILABLE, status=503) from None
    if contract != BOOKING_RPC_PENDING_V7:
        raise PaymentExpiryError(SAFE_UNAVAILABLE, status=503)


def run_expire_abandoned_payment_sessions(supabase) -> dict:
    """Call the SQL sweeper and return only validated integer counters."""
    try:
        res = supabase.rpc(EXPIRE_RPC_NAME, {}).execute()
    except Exception as exc:  # noqa: BLE001
        logger.error("%s failed: %s", EXPIRE_RPC_NAME, type(exc).__name__)
        raise PaymentExpiryError(SAFE_FAILED, status=500) from None
    return expiry_success_body(_rpc_payload(res))


def expiry_success_body(payload) -> dict:
    counters = validate_expire_rpc_result(payload)
    body = {"ok": True, **counters}
    return _checked_body(body)


def expiry_error_body(exc: PaymentExpiryError) -> dict:
    return _checked_body({"ok": False, "error": exc.user_message})


def validate_expire_rpc_result(payload) -> dict:
    if not isinstance(payload, Mapping):
        raise PaymentExpiryError(SAFE_FAILED, status=500)
    out = {}
    for key in SAFE_COUNTER_KEYS:
        value = payload.get(key)
        if type(value) is not int or isinstance(value, bool) or value < 0:
            raise PaymentExpiryError(SAFE_FAILED, status=500)
        out[key] = value
    return out


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


def _checked_body(body: dict) -> dict:
    leaked = leaked_internal_keys(body)
    if leaked:
        raise RuntimeError("internal keys leaked from expiry caller")
    extra = set(body) - SAFE_RESPONSE_KEYS
    if extra:
        raise RuntimeError("unexpected expiry caller fields")
    return body
