"""Server-only payment-session helpers for public booking.

The browser may receive a raw payment-session token once. PostgreSQL stores
only the SHA-256 hex. Internal booking identifiers never belong in JSON,
redirect URLs, templates, logs, or email.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from typing import Optional

BOOKING_RPC_LIVE_V6 = "live_v6"
BOOKING_RPC_PENDING_V7 = "pending_v7"


class BookingRpcContractError(Exception):
    """CREATE_PUBLIC_BOOKING_CONTRACT is set to an unsupported value.

    Absent/blank defaults to live_v6. Only exact live_v6 and pending_v7 are
    valid explicit values. Unknown values must not fall back to live_v6.
    """

INTERNAL_ONLY_KEYS = frozenset(
    {
        "canonical_booking_id",
        "reservation_id",
        "payment_session_id",
        "session_token_hash",
        "session_id",
        "booking_id",
        "guest_id",
        "confirmation_token",
        "cancellation_token",
        "cancellation_token_hash",
        "session_status",
        "expires_at",
        "already_progressed",
        "processing_started_at",
        "moneris_payment_method_id",
        "moneris_issuer_id",
        "paymentMethodId",
        "issuerId",
        "dataKey",
        "data_key",
        "claim_id",
        "current_registration_idempotency_key",
        "registration_idempotency_key",
    }
)

# Fields Flask may keep after create_public_booking. confirmation_token is for
# the live_v6 confirmation page/email only and must not be copied into a
# pending_v7 browser payload.
SERVER_CREATE_RESULT_KEYS = frozenset(
    {
        "ok",
        "booking_reference",
        "confirmation_token",
        "reused",
        "token_rotated",
        "error",
    }
)

BROWSER_CREATE_KEYS = frozenset(
    {
        "success",
        "booking_reference",
        "reused",
        "payment_session_token",
        "next_step",
        "payment_url",
        "nights",
        "subtotal",
        "gst",
        "atl",
        "grand_total",
        "error",
    }
)


def booking_rpc_contract() -> str:
    """Which create_public_booking overload Flask will call.

    live_v6: current production 6-arg RPC (inserts confirmed). Default so a
    website deploy cannot invoke the missing 7-arg function before sql/003.
    pending_v7: future 7-arg RPC from sql/003 (pending_payment + session).

    Absent or blank env -> live_v6. Exact "live_v6" / "pending_v7" only.
    Any other explicit value raises BookingRpcContractError (fail closed).
    Aliases such as "v7" or "pending" are rejected. Do not try both
    overloads in one request.
    """
    raw = os.getenv("CREATE_PUBLIC_BOOKING_CONTRACT")
    if raw is None or not str(raw).strip():
        return BOOKING_RPC_LIVE_V6
    value = str(raw).strip()
    if value == BOOKING_RPC_LIVE_V6:
        return BOOKING_RPC_LIVE_V6
    if value == BOOKING_RPC_PENDING_V7:
        return BOOKING_RPC_PENDING_V7
    raise BookingRpcContractError("invalid CREATE_PUBLIC_BOOKING_CONTRACT")


def uses_pending_payment_rpc() -> bool:
    return booking_rpc_contract() == BOOKING_RPC_PENDING_V7


def generate_payment_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_payment_session_token(token: str) -> str:
    if not isinstance(token, str) or not token:
        raise ValueError("payment session token is required")
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if len(digest) != 64:
        raise ValueError("payment session token hash is invalid")
    return digest


def is_session_token_hash(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= set("0123456789abcdef")


def leaked_internal_keys(payload) -> set:
    """Return internal-only key names found anywhere in a nested payload."""
    found = set()

    def walk(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key in INTERNAL_ONLY_KEYS:
                    found.add(key)
                walk(value)
        elif isinstance(obj, (list, tuple)):
            for item in obj:
                walk(item)

    walk(payload)
    return found


def payment_session_token_for_browser(*, token_rotated, raw_token: Optional[str]):
    """Raw token is returned only when the RPC rotated the stored hash.

    First create and OPEN replay set token_rotated=true. PROCESSING reuse
    sets token_rotated=false so the browser keeps the token it already has.
    """
    if token_rotated and raw_token:
        return raw_token
    return None


def server_create_booking_result(rpc_payload: dict) -> dict:
    """Keep only server-side create_public_booking fields. Drops internal IDs."""
    if not rpc_payload or not rpc_payload.get("booking_reference"):
        return {"ok": False, "error": "Could not store your booking. Please try again."}
    out = {
        "ok": True,
        "booking_reference": rpc_payload.get("booking_reference"),
        "confirmation_token": rpc_payload.get("confirmation_token"),
        "reused": bool(rpc_payload.get("reused")),
        "token_rotated": bool(rpc_payload.get("token_rotated")),
    }
    leaked = leaked_internal_keys(out) - {"confirmation_token"}
    if leaked:
        raise RuntimeError("internal keys leaked from RPC result")
    extra = set(out) - SERVER_CREATE_RESULT_KEYS
    if extra:
        raise RuntimeError("unexpected create-booking server fields")
    return out


def to_browser_booking_create_response(
    rpc_payload: dict,
    *,
    raw_payment_session_token: Optional[str],
) -> dict:
    """Copy only browser-safe create-booking fields. Never copies internal IDs."""
    if not rpc_payload or rpc_payload.get("ok") is False:
        return {
            "success": False,
            "error": (rpc_payload or {}).get("error") or "booking_failed",
        }

    out = {
        "success": True,
        "booking_reference": rpc_payload.get("booking_reference"),
        "reused": bool(rpc_payload.get("reused")),
    }
    token = payment_session_token_for_browser(
        token_rotated=rpc_payload.get("token_rotated"),
        raw_token=raw_payment_session_token,
    )
    if token:
        out["payment_session_token"] = token
    leaked = leaked_internal_keys(out)
    if leaked:
        raise RuntimeError("internal keys leaked to browser")
    extra = set(out) - BROWSER_CREATE_KEYS
    if extra:
        raise RuntimeError("unexpected browser create-booking fields")
    return out


def pending_payment_browser_payload(
    rpc_payload: dict,
    *,
    raw_payment_session_token: Optional[str],
    nights,
    subtotal,
    gst,
    atl,
    grand_total,
) -> dict:
    body = to_browser_booking_create_response(
        rpc_payload, raw_payment_session_token=raw_payment_session_token
    )
    if not body.get("success"):
        return body
    body.update(
        {
            "next_step": "payment",
            "payment_url": "/complete-payment",
            "nights": nights,
            "subtotal": subtotal,
            "gst": gst,
            "atl": atl,
            "grand_total": grand_total,
        }
    )
    leaked = leaked_internal_keys(body)
    if leaked:
        raise RuntimeError("internal keys leaked to browser")
    extra = set(body) - BROWSER_CREATE_KEYS
    if extra:
        raise RuntimeError("unexpected browser create-booking fields")
    return body
