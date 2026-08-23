"""Server-side pending-payment completion.

The browser may send only ``payment_session_token`` and ``dataKey``. The
payment-session token is the authorization capability. Internal identifiers
come only from ``claim_booking_payment_session`` after the hash lookup.

Claim is a short database transaction and must finish before any Moneris
HTTP. Registration idempotency key is the claimed
``current_registration_idempotency_key``, not ``session_id``.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Callable, Optional

from payment_session import (
    BOOKING_RPC_PENDING_V7,
    BookingRpcContractError,
    booking_rpc_contract,
    hash_payment_session_token,
    leaked_internal_keys,
)

logger = logging.getLogger(__name__)

# Match payment_api.validation temporaryToken bounds. Duplicated so this
# website module does not import the payments API at process start.
DATA_KEY_MIN_LENGTH = 25
DATA_KEY_MAX_LENGTH = 28
PAYMENT_SESSION_TOKEN_MIN_LENGTH = 16
PAYMENT_SESSION_TOKEN_MAX_LENGTH = 128

ALLOWED_BROWSER_KEYS = frozenset({"payment_session_token", "dataKey"})
BROWSER_COMPLETE_KEYS = frozenset(
    {
        "success",
        "booking_reference",
        "redirect_url",
        "email_sent",
        "error",
        "retry_payment",
    }
)

SAFE_UNAVAILABLE = "Payment is not available."
SAFE_CONFIG = "Online booking is temporarily unavailable."
SAFE_INVALID = "We could not complete this payment. Please try again."
SAFE_EXPIRED = "This payment session has expired. Please start a new booking."
SAFE_IN_PROGRESS = (
    "Payment is still being processed. Please wait a moment and try again."
)
SAFE_DECLINED = "Card validation was rejected"
SAFE_RECONCILIATION = (
    "We could not confirm your payment. Your reservation has not been "
    "cancelled. Please call the lodge at 780-827-2007."
)
SAFE_CONFLICT = "A payment credential already exists for this booking"

# Injected by tests. Production uses default_register_booking_payment_credential.
register_credential_fn: Optional[Callable] = None


class PaymentCompletionError(Exception):
    """Fail-closed payment completion with a browser-safe message."""

    def __init__(self, message: str, *, status: int = 400, retry_payment: bool = False):
        super().__init__(message)
        self.user_message = message
        self.status = status
        self.retry_payment = bool(retry_payment)


def require_pending_v7_contract() -> None:
    """Payment completion exists only under the sql/003 7-arg contract."""
    try:
        contract = booking_rpc_contract()
    except BookingRpcContractError:
        raise PaymentCompletionError(SAFE_CONFIG, status=503) from None
    if contract != BOOKING_RPC_PENDING_V7:
        raise PaymentCompletionError(SAFE_UNAVAILABLE, status=404)


def parse_browser_payment_request(payload) -> tuple[str, str]:
    """Accept only payment_session_token + dataKey. Reject extra keys.

    Failures here occur before claim, so the payment session is still OPEN.
    ``retry_payment=True`` is explicit: the browser may tokenize a new card.
    """
    if not isinstance(payload, dict):
        raise PaymentCompletionError(SAFE_INVALID, status=400, retry_payment=True)
    if set(payload.keys()) != ALLOWED_BROWSER_KEYS:
        raise PaymentCompletionError(SAFE_INVALID, status=400, retry_payment=True)
    token = payload.get("payment_session_token")
    data_key = payload.get("dataKey")
    if not isinstance(token, str) or not isinstance(data_key, str):
        raise PaymentCompletionError(SAFE_INVALID, status=400, retry_payment=True)
    token = token.strip()
    data_key = data_key.strip()
    if not (
        PAYMENT_SESSION_TOKEN_MIN_LENGTH
        <= len(token)
        <= PAYMENT_SESSION_TOKEN_MAX_LENGTH
    ):
        raise PaymentCompletionError(SAFE_INVALID, status=400, retry_payment=True)
    if not (DATA_KEY_MIN_LENGTH <= len(data_key) <= DATA_KEY_MAX_LENGTH):
        raise PaymentCompletionError(SAFE_INVALID, status=400, retry_payment=True)
    return token, data_key


PAYMENT_API_ENV_NAMES = frozenset(
    {
        "MONERIS_ENV",
        "MONERIS_CLIENT_ID",
        "MONERIS_CLIENT_SECRET",
        "MONERIS_MERCHANT_ID",
        "MONERIS_API_VERSION",
        "MONERIS_API_BASE_URL",
        "MONERIS_HOSTED_TOKENIZATION_PROFILE_ID",
        "MONERIS_HOSTED_TOKENIZATION_URL",
        "SUPABASE_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
    }
)


def _payment_api_environ():
    """Allowlisted environ for payment_api.load_config.

    Only names load_config requires are copied. Optional QA/CORS flags are
    omitted so the website registrar cannot enable the QA bridge.
    payment_api requires SUPABASE_SERVICE_ROLE_KEY. The website already
    accepts SUPABASE_SECRET_KEY / SUPABASE_KEY as that same server
    credential. Values are not logged.
    """
    environ = {}
    for name in PAYMENT_API_ENV_NAMES:
        raw = os.environ.get(name)
        if isinstance(raw, str):
            environ[name] = raw
    if environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip():
        return environ
    for name in ("SUPABASE_SECRET_KEY", "SUPABASE_KEY"):
        alias = os.environ.get(name)
        if isinstance(alias, str) and alias.strip():
            environ["SUPABASE_SERVICE_ROLE_KEY"] = alias
            break
    return environ


def default_register_booking_payment_credential(
    canonical_booking_id,
    data_key,
    idempotency_key,
):
    """Call the existing payments-API registrar. Imported lazily."""
    try:
        from payment_api.config import load_config
        from payment_api.credential_registration import (
            register_booking_payment_credential,
        )
        from payment_api.errors import PaymentConfigError
    except ImportError:
        raise PaymentCompletionError(SAFE_UNAVAILABLE, status=503) from None
    try:
        config = load_config(_payment_api_environ())
    except PaymentConfigError:
        logger.error("payment_api config invalid: %s", "PaymentConfigError")
        raise PaymentCompletionError(SAFE_UNAVAILABLE, status=503) from None
    return register_booking_payment_credential(
        config,
        canonical_booking_id,
        data_key,
        idempotency_key,
    )


def _rpc_payload(res):
    data = getattr(res, "data", None)
    if isinstance(data, list):
        return data[0] if data else {}
    return data or {}


# Server-defined RAISE EXCEPTION texts from sql/003. postgrest.APIError.code
# is SQLSTATE P0001 for these raises and is not a discriminator. Map only
# the structured APIError.message (exact identifier), never str(exc)
# substrings and never Moneris processor text.
DB_RPC_ERROR_IDENTIFIERS = frozenset(
    {
        "payment_session_expired",
        "payment_session_stale_processing",
        "payment_session_not_found",
        "invalid_session_token_hash",
        "payment_session_not_open",
        "credential_not_succeeded",
        "reservation_not_pending_payment",
        "payment_session_not_finalizable",
        "confirmation_email_not_found",
        "invalid_reservation_id",
        "stale_email_claim",
        "payment_session_not_processing",
        "stale_registration_attempt",
        "registration_not_failed",
    }
)


def db_rpc_error_identifier(exc: Exception):
    """Exact DB RPC identifier from PostgREST structured fields.

    Uses APIError.message, or APIError.code only when that code is itself
    one of the sql/003 identifiers. Does not parse str(exc). Does not infer
    Moneris outcomes.
    """
    message = getattr(exc, "message", None)
    if isinstance(message, str) and message in DB_RPC_ERROR_IDENTIFIERS:
        return message
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code in DB_RPC_ERROR_IDENTIFIERS:
        return code
    return None


def _claim_error_from_exception(exc: Exception) -> PaymentCompletionError:
    identifier = db_rpc_error_identifier(exc)
    if identifier == "payment_session_expired":
        return PaymentCompletionError(SAFE_EXPIRED, status=409)
    if identifier == "payment_session_stale_processing":
        return PaymentCompletionError(SAFE_IN_PROGRESS, status=409)
    if identifier == "payment_session_not_found":
        return PaymentCompletionError(SAFE_INVALID, status=404)
    if identifier == "invalid_session_token_hash":
        return PaymentCompletionError(SAFE_INVALID, status=400)
    if identifier == "payment_session_not_open":
        return PaymentCompletionError(SAFE_INVALID, status=409)
    return PaymentCompletionError(SAFE_INVALID, status=500)


def _require_uuid(value):
    if value is None:
        raise PaymentCompletionError(SAFE_INVALID, status=500)
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise PaymentCompletionError(SAFE_INVALID, status=500) from None


def _exc_named(exc: Exception, *names: str) -> bool:
    for cls in type(exc).__mro__:
        if cls.__name__ in names:
            return True
    return False


def _map_registration_exception(exc: Exception) -> PaymentCompletionError:
    """Map typed registration errors. Never branch on message text."""
    if _exc_named(exc, "CredentialPersistenceError"):
        return PaymentCompletionError(SAFE_RECONCILIATION, status=502)
    if _exc_named(exc, "CredentialConflictError"):
        return PaymentCompletionError(SAFE_CONFLICT, status=409)
    if _exc_named(exc, "MonerisAuthError"):
        return PaymentCompletionError(SAFE_RECONCILIATION, status=502)
    if _exc_named(exc, "MonerisValidationError"):
        category = getattr(exc, "category", None)
        if category == "CONFIRMED_DECLINE":
            return PaymentCompletionError(SAFE_DECLINED, status=422)
        if category == "INVALID_REQUEST":
            return PaymentCompletionError(SAFE_INVALID, status=400)
        return PaymentCompletionError(SAFE_RECONCILIATION, status=502)
    if _exc_named(exc, "CredentialRegistrationError"):
        return PaymentCompletionError(SAFE_INVALID, status=422)
    return PaymentCompletionError(SAFE_INVALID, status=500)


def _registration_status(result) -> str:
    if result is None:
        return ""
    return str(getattr(result, "registration_status", "") or "")


def _load_confirmation_access(supabase, reservation_id: str):
    """Trusted server read of confirmation_token after claim/finalize.

    The public confirmation page still requires this token. booking_reference
    alone is never sufficient. Uses reservation_id from the claim/finalize
    result, never a browser-supplied id.
    """
    try:
        res = (
            supabase.table("bookings")
            .select("booking_reference, confirmation_token")
            .eq("reservation_id", reservation_id)
            .limit(1)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("confirmation access lookup failed: %s", type(exc).__name__)
        raise PaymentCompletionError(SAFE_INVALID, status=500) from None
    rows = getattr(res, "data", None) or []
    if not rows:
        raise PaymentCompletionError(SAFE_INVALID, status=500)
    row = rows[0]
    booking_reference = row.get("booking_reference")
    confirmation_token = row.get("confirmation_token")
    if not booking_reference or not confirmation_token:
        raise PaymentCompletionError(SAFE_INVALID, status=500)
    return booking_reference, confirmation_token


def _browser_success(*, booking_reference: str, confirmation_token: str, email_sent: bool):
    body = {
        "success": True,
        "booking_reference": booking_reference,
        "redirect_url": (
            f"/reservation-confirmation/{booking_reference}"
            f"?token={confirmation_token}"
        ),
        "email_sent": bool(email_sent),
    }
    leaked = leaked_internal_keys(body)
    if leaked:
        raise RuntimeError("internal keys leaked to browser")
    extra = set(body) - BROWSER_COMPLETE_KEYS
    if extra:
        raise RuntimeError("unexpected payment-complete browser fields")
    return body


def payment_completion_error_body(exc: PaymentCompletionError) -> dict:
    """Browser-safe error JSON. retry_payment only after explicit server OK."""
    body = {"success": False, "error": exc.user_message}
    if exc.retry_payment:
        body["retry_payment"] = True
    leaked = leaked_internal_keys(body)
    if leaked:
        raise RuntimeError("internal keys leaked to browser")
    extra = set(body) - BROWSER_COMPLETE_KEYS
    if extra:
        raise RuntimeError("unexpected payment-complete browser fields")
    return body


def claim_booking_payment_session(supabase, session_token_hash: str) -> dict:
    """OPEN -> PROCESSING. Caller must not start Moneris until this returns."""
    try:
        res = supabase.rpc(
            "claim_booking_payment_session",
            {"p_session_token_hash": session_token_hash},
        ).execute()
    except Exception as exc:  # noqa: BLE001
        logger.error("claim_booking_payment_session failed: %s", type(exc).__name__)
        raise _claim_error_from_exception(exc) from None
    data = _rpc_payload(res)
    if not data.get("ok"):
        raise PaymentCompletionError(SAFE_INVALID, status=500)
    return data


def finalize_booking_after_credential(supabase, session_id: str) -> dict:
    try:
        res = supabase.rpc(
            "finalize_booking_after_credential",
            {"p_session_id": session_id},
        ).execute()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "finalize_booking_after_credential failed: %s", type(exc).__name__
        )
        identifier = db_rpc_error_identifier(exc)
        if identifier == "credential_not_succeeded":
            raise PaymentCompletionError(SAFE_INVALID, status=409) from None
        if identifier == "payment_session_expired":
            raise PaymentCompletionError(SAFE_EXPIRED, status=409) from None
        raise PaymentCompletionError(SAFE_INVALID, status=500) from None
    data = _rpc_payload(res)
    if not data.get("ok"):
        raise PaymentCompletionError(SAFE_INVALID, status=500)
    return data


def _rpc_json(supabase, name: str, args: dict) -> dict:
    res = supabase.rpc(name, args).execute()
    return _rpc_payload(res)


def _reopen_after_failed_registration(supabase, session_id: str, attempt_key: str) -> bool:
    """True only when the DB verified FAILED and reopened to OPEN."""
    try:
        data = _rpc_json(
            supabase,
            "reopen_payment_session_after_failed_registration",
            {
                "p_session_id": session_id,
                "p_registration_idempotency_key": attempt_key,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "reopen_payment_session_after_failed_registration failed: %s",
            type(exc).__name__,
        )
        return False
    return bool(data.get("ok"))


def _attempt_confirmation_email(
    *,
    supabase,
    reservation_id: str,
    booking_reference: str,
    confirmation_token: str,
    fetch_confirmation,
    send_email,
) -> bool:
    """Claim, SMTP, then mark SENT. Never holds a DB txn across SMTP.

    Returns True when this process believes the provider accepted the mail,
    including the case where mark-sent fails after a successful provider
    call (at-least-once ambiguity). Provider failure releases the claim so
    a later retry can send. Booking status is never changed here.
    """
    try:
        claim = _rpc_json(
            supabase,
            "claim_reservation_confirmation_email",
            {"p_reservation_id": reservation_id},
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "claim_reservation_confirmation_email failed: %s", type(exc).__name__
        )
        return False
    if not claim.get("should_send"):
        return bool(claim.get("already_sent"))

    try:
        claim_id = _require_uuid(claim.get("claim_id"))
    except PaymentCompletionError:
        logger.error("email claim missing claim_id")
        return False

    sent = False
    try:
        confirmation = fetch_confirmation(booking_reference, confirmation_token)
        if confirmation:
            sent, _err = send_email(confirmation)
            sent = bool(sent)
    except Exception as exc:  # noqa: BLE001
        logger.error("confirmation email provider failed: %s", type(exc).__name__)
        sent = False

    if sent:
        try:
            _rpc_json(
                supabase,
                "mark_reservation_confirmation_email_sent",
                {
                    "p_reservation_id": reservation_id,
                    "p_claim_id": claim_id,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "mark_reservation_confirmation_email_sent failed: %s",
                type(exc).__name__,
            )
            # Provider succeeded; persistent flag may still be SENDING.
            # Retry after the 2-minute lease may duplicate. Prefer duplicate
            # over silent loss. A stale_email_claim here means another
            # worker owns the lease; do not mutate that lease.
        return True

    try:
        _rpc_json(
            supabase,
            "release_reservation_confirmation_email_claim",
            {
                "p_reservation_id": reservation_id,
                "p_claim_id": claim_id,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "release_reservation_confirmation_email_claim failed: %s",
            type(exc).__name__,
        )
    return False


def complete_pending_payment(
    *,
    payment_session_token: str,
    data_key: str,
    supabase,
    fetch_confirmation,
    send_email,
    register_credential: Optional[Callable] = None,
) -> dict:
    """Claim, register, finalize, then email/redirect.

    ``register_credential(canonical_booking_id, data_key, attempt_key)`` is the
    existing ``register_booking_payment_credential`` contract. Tests inject a
    mock. This function never cancels a reservation.
    """
    require_pending_v7_contract()
    session_token_hash = hash_payment_session_token(payment_session_token)
    claim = claim_booking_payment_session(supabase, session_token_hash)

    session_id = _require_uuid(claim.get("session_id"))
    canonical_booking_id = _require_uuid(claim.get("canonical_booking_id"))
    reservation_id = _require_uuid(claim.get("reservation_id"))

    if claim.get("already_finalized") or claim.get("session_status") == "CONSUMED":
        booking_reference, confirmation_token = _load_confirmation_access(
            supabase, reservation_id
        )
        email_sent = _attempt_confirmation_email(
            supabase=supabase,
            reservation_id=reservation_id,
            booking_reference=booking_reference,
            confirmation_token=confirmation_token,
            fetch_confirmation=fetch_confirmation,
            send_email=send_email,
        )
        return _browser_success(
            booking_reference=booking_reference,
            confirmation_token=confirmation_token,
            email_sent=email_sent,
        )

    if claim.get("already_claimed"):
        raise PaymentCompletionError(SAFE_IN_PROGRESS, status=409)

    attempt_key = _require_uuid(claim.get("current_registration_idempotency_key"))

    register = register_credential or register_credential_fn
    if register is None:
        register = default_register_booking_payment_credential

    try:
        result = register(canonical_booking_id, data_key, attempt_key)
    except PaymentCompletionError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("credential registration failed: %s", type(exc).__name__)
        mapped = _map_registration_exception(exc)
        if _reopen_after_failed_registration(supabase, session_id, attempt_key):
            raise PaymentCompletionError(
                SAFE_DECLINED, status=422, retry_payment=True
            ) from None
        raise mapped from None

    status = _registration_status(result)
    if status == "RECONCILIATION_REQUIRED":
        raise PaymentCompletionError(SAFE_RECONCILIATION, status=502)
    if status == "FAILED":
        if _reopen_after_failed_registration(supabase, session_id, attempt_key):
            raise PaymentCompletionError(
                SAFE_DECLINED, status=422, retry_payment=True
            )
        raise PaymentCompletionError(SAFE_DECLINED, status=422)
    if status != "SUCCEEDED":
        raise PaymentCompletionError(SAFE_INVALID, status=500)

    finalize = finalize_booking_after_credential(supabase, session_id)
    booking_reference, confirmation_token = _load_confirmation_access(
        supabase, reservation_id
    )
    if finalize.get("booking_reference"):
        booking_reference = finalize.get("booking_reference") or booking_reference

    email_sent = _attempt_confirmation_email(
        supabase=supabase,
        reservation_id=reservation_id,
        booking_reference=booking_reference,
        confirmation_token=confirmation_token,
        fetch_confirmation=fetch_confirmation,
        send_email=send_email,
    )

    return _browser_success(
        booking_reference=booking_reference,
        confirmation_token=confirmation_token,
        email_sent=email_sent,
    )
