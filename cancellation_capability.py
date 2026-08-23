"""Pending_v7 confirmation-email cancellation capability.

Derives a deterministic raw cancellation token from a dedicated server
secret and reservation_id. Postgres continues to store only SHA-256(raw).
The raw token is never persisted, logged, or returned to the browser.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import uuid
from typing import Optional


SECRET_ENV = "CANCELLATION_TOKEN_SECRET"
MIN_SECRET_LENGTH = 32
MAC_CONTEXT = b"gml-cancellation-v1|"
PLACEHOLDER_MAC_CONTEXT = b"gml-cancellation-placeholder-v1|"

MATCH_CURRENT = "match_current"
MATCH_PREVIOUS = "match_previous"
PLACEHOLDER = "placeholder"
INCOMPATIBLE = "incompatible"


class CancellationCapabilityError(Exception):
    """Fail-closed cancellation capability persist/derive error."""


def configured_cancellation_token_secret() -> Optional[str]:
    """Return the dedicated secret, or None if missing/too short.

    Does not fall back to FLASK_SECRET_KEY or any other secret.
    """
    raw = os.getenv(SECRET_ENV)
    if raw is None:
        return None
    secret = str(raw).strip()
    if len(secret) < MIN_SECRET_LENGTH:
        return None
    return secret


def canonical_reservation_id(value) -> str:
    """Lowercase hyphenated UUID string. Rejects non-UUID values."""
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise CancellationCapabilityError("invalid_reservation_id") from exc


def _require_secret(secret: str) -> str:
    if not isinstance(secret, str) or len(secret) < MIN_SECRET_LENGTH:
        raise CancellationCapabilityError("invalid_secret")
    return secret


def _urlsafe_hmac(secret: str, message: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def derive_cancellation_token(secret: str, reservation_id) -> str:
    """HMAC-SHA256 raw token for one reservation. Stdlib only."""
    secret = _require_secret(secret)
    rid = canonical_reservation_id(reservation_id)
    return _urlsafe_hmac(secret, MAC_CONTEXT + rid.encode("ascii"))


def canonical_booking_reference(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CancellationCapabilityError("invalid_booking_reference")
    return value.strip().upper()


def create_placeholder_hash(secret: str, booking_reference) -> str:
    """SHA-256 of the pending_v7 create-time placeholder token.

    Distinct MAC context from the email-time capability token. The raw
    placeholder token is never emailed or stored.
    """
    secret = _require_secret(secret)
    ref = canonical_booking_reference(booking_reference)
    raw = _urlsafe_hmac(secret, PLACEHOLDER_MAC_CONTEXT + ref.encode("ascii"))
    return hash_cancellation_token(raw)


def hash_cancellation_token(token: str) -> str:
    """SHA-256 hex of the raw token string. Matches main._hash_cancellation_token."""
    if not isinstance(token, str) or not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def cancellation_url(booking_reference: str, raw_token: str) -> str:
    base = (os.getenv("PUBLIC_SITE_URL") or "https://grandemountainlodge.com").rstrip(
        "/"
    )
    return f"{base}/cancel-reservation/{booking_reference}?token={raw_token}"


def _is_sha256_hex(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= set(
        "0123456789abcdef"
    )


def classify_stored_hash(
    stored,
    current_hash: str,
    *,
    expected_placeholder_hash: Optional[str] = None,
    previous_hash: Optional[str] = None,
) -> str:
    """Classify an existing cancellation_token_hash for persist.

    PLACEHOLDER only when stored equals the re-derived create-time hash.
    Any other well-formed SHA-256 is INCOMPATIBLE, not a placeholder.
    previous_hash is reserved for a future dual-secret rotation.
    """
    if not _is_sha256_hex(current_hash) or not _is_sha256_hex(stored):
        return INCOMPATIBLE
    if stored == current_hash:
        return MATCH_CURRENT
    if previous_hash and _is_sha256_hex(previous_hash) and stored == previous_hash:
        return MATCH_PREVIOUS
    if (
        expected_placeholder_hash
        and _is_sha256_hex(expected_placeholder_hash)
        and stored == expected_placeholder_hash
    ):
        return PLACEHOLDER
    return INCOMPATIBLE


def persist_cancellation_capability_hash(
    supabase,
    reservation_id,
    token_hash: str,
    *,
    expected_placeholder_hash: Optional[str] = None,
    previous_hash: Optional[str] = None,
) -> str:
    """Write or verify the unused cancellation hash for one reservation.

    Resolves reservation_id -> first booking_id (ORDER BY booking_id) ->
    public.cancellation.booking_id. Updates only cancellation_token_hash.
    Returns the classify decision. Raises CancellationCapabilityError
    without mutating token_expiry / token_used_at / token_usage.
    """
    if not _is_sha256_hex(token_hash):
        raise CancellationCapabilityError("invalid_hash")
    rid = canonical_reservation_id(reservation_id)

    try:
        booking_res = (
            supabase.table("bookings")
            .select("booking_id")
            .eq("reservation_id", rid)
            .order("booking_id")
            .limit(1)
            .execute()
        )
    except CancellationCapabilityError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CancellationCapabilityError("lookup_failed") from exc
    booking_rows = getattr(booking_res, "data", None) or []
    if not booking_rows:
        raise CancellationCapabilityError("booking_not_found")
    booking_id = booking_rows[0].get("booking_id")
    if booking_id is None:
        raise CancellationCapabilityError("booking_not_found")

    try:
        cancel_res = (
            supabase.table("cancellation")
            .select(
                "id, booking_id, cancellation_token_hash, token_usage, "
                "token_expiry, token_used_at"
            )
            .eq("booking_id", booking_id)
            .limit(2)
            .execute()
        )
    except CancellationCapabilityError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CancellationCapabilityError("lookup_failed") from exc
    cancel_rows = getattr(cancel_res, "data", None) or []
    if len(cancel_rows) != 1:
        raise CancellationCapabilityError("cancellation_not_unique")
    row = cancel_rows[0]
    if str(row.get("booking_id")) != str(booking_id):
        raise CancellationCapabilityError("booking_mismatch")
    if row.get("token_usage") is True:
        raise CancellationCapabilityError("already_used")

    stored_hash = row.get("cancellation_token_hash")
    decision = classify_stored_hash(
        stored_hash,
        token_hash,
        expected_placeholder_hash=expected_placeholder_hash,
        previous_hash=previous_hash,
    )
    if decision == MATCH_CURRENT:
        return decision
    if decision != PLACEHOLDER:
        raise CancellationCapabilityError("incompatible_hash")

    row_id = row.get("id")
    if not row_id or not _is_sha256_hex(stored_hash):
        raise CancellationCapabilityError("cancellation_not_unique")
    try:
        updated = (
            supabase.table("cancellation")
            .update({"cancellation_token_hash": token_hash})
            .eq("id", row_id)
            .eq("booking_id", booking_id)
            .eq("token_usage", False)
            .eq("cancellation_token_hash", stored_hash)
            .execute()
        )
    except CancellationCapabilityError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CancellationCapabilityError("persist_failed") from exc
    written = getattr(updated, "data", None) or []
    if len(written) != 1:
        try:
            verify = (
                supabase.table("cancellation")
                .select(
                    "cancellation_token_hash, token_expiry, token_used_at, "
                    "token_usage"
                )
                .eq("id", row_id)
                .eq("booking_id", booking_id)
                .limit(1)
                .execute()
            )
        except CancellationCapabilityError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise CancellationCapabilityError("persist_failed") from exc
        verified = getattr(verify, "data", None) or []
        if (
            len(verified) == 1
            and verified[0].get("token_usage") is not True
            and verified[0].get("cancellation_token_hash") == token_hash
        ):
            return MATCH_CURRENT
        raise CancellationCapabilityError("persist_conflict")
    if written[0].get("cancellation_token_hash") != token_hash:
        raise CancellationCapabilityError("persist_failed")
    if written[0].get("token_usage") is True:
        raise CancellationCapabilityError("persist_failed")
    return decision
