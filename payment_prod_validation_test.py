"""TEMPORARY production real-card validation test.

TEMPORARY — remove this module immediately after the authorized
production card-validation test. It is not a public booking path.

Purpose: one deliberately authorized operator POST may create exactly one
QA pending_payment reservation, mint a capability bound to THAT payment
session, and let the existing /complete-payment page plus
/api/complete-payment run while DIRECT_BOOKINGS_PAUSED stays True.

This module does not purchase, capture, or refund. It does not call the
Moneris payments endpoint. It does not instantiate a Moneris client. After
the QA session exists, payment processing is the existing
parse_browser_payment_request and complete_pending_payment path
(Card Validation /validations only).

Capability design (option A — signed cookie, no new Supabase table):
HMAC-SHA256 over a random nonce, the SHA-256 of the opaque payment-session
token, and an expiry. Integrity is protected by
PAYMENT_PROD_VALIDATION_TEST_SECRET. Only the hash is in the cookie, never
the raw session token, test secret, booking UUID, reservation UUID,
payment_session UUID, Moneris IDs, or dataKey. No in-memory dict (unsafe
across Vercel instances). No new schema.

The admin/test secret is never the browser capability.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

from payment_ht import (
    HostedTokenizationConfigError,
    load_hosted_tokenization_browser_config,
)
from payment_session import (
    hash_payment_session_token,
    is_session_token_hash,
)

logger = logging.getLogger(__name__)

# TEMPORARY — delete these names with this module after the test.
SECRET_ENV = "PAYMENT_PROD_VALIDATION_TEST_SECRET"
EMAIL_ENV = "PAYMENT_PROD_VALIDATION_TEST_EMAIL"
MIN_SECRET_LENGTH = 32
COOKIE_NAME = "gml_prod_val_cap"
COOKIE_PATH = "/"
CAPABILITY_TTL_SECONDS = 20 * 60  # <= payment-session TTL
MAC_CONTEXT = "gml-prod-val-v1"
START_PATH = "/internal/prod-card-validation-test"

FORBIDDEN_REUSED_SECRET_ENVS = (
    "PAYMENT_RECONCILIATION_ADMIN_SECRET",
    "PAYMENT_EXPIRY_CRON_SECRET",
    "CANCELLATION_TOKEN_SECRET",
)

QA_MIN_LEAD_DAYS = 60
QA_MAX_LEAD_DAYS = 90

QA_FIRST_NAME = "PRODUCTION"
QA_LAST_NAME = "QA VALIDATION"
QA_SPECIAL_REQUESTS = "PRODUCTION CARD-VALIDATION TEST — DELETE AFTER TEST"
QA_PHONE = "000-000-0000"
QA_ADDRESS = "QA TEST ADDRESS"
QA_CITY = "Grande Cache"
QA_COUNTRY = "Canada"

SAFE_UNAVAILABLE = "Production card-validation test is unavailable."
SAFE_UNAUTHORIZED = "Unauthorized."

# TEMPORARY server-only persist categories. The public /confirm-booking
# route must never copy persist_diag into a browser response.
PERSIST_ASSIGN_FAILED = "PERSIST_ASSIGN_FAILED"
PERSIST_ROOM_UNAVAILABLE = "PERSIST_ROOM_UNAVAILABLE"
PERSIST_GUEST_EMAIL_CONFLICT = "PERSIST_GUEST_EMAIL_CONFLICT"
PERSIST_RESERVATION_EXPIRED = "PERSIST_RESERVATION_EXPIRED"
PERSIST_STALE_PROCESSING = "PERSIST_STALE_PROCESSING"
PERSIST_STATE_INCONSISTENT = "PERSIST_STATE_INCONSISTENT"
PERSIST_RPC_GENERIC = "PERSIST_RPC_GENERIC"
PERSIST_NO_BOOKING_REFERENCE = "PERSIST_NO_BOOKING_REFERENCE"
PERSIST_OTHER = "PERSIST_OTHER"

SAFE_PERSIST_DIAG_CODES = frozenset(
    {
        PERSIST_ASSIGN_FAILED,
        PERSIST_ROOM_UNAVAILABLE,
        PERSIST_GUEST_EMAIL_CONFLICT,
        PERSIST_RESERVATION_EXPIRED,
        PERSIST_STALE_PROCESSING,
        PERSIST_STATE_INCONSISTENT,
        PERSIST_RPC_GENERIC,
        PERSIST_NO_BOOKING_REFERENCE,
        PERSIST_OTHER,
    }
)

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class ProdValidationTestError(Exception):
    """TEMPORARY fail-closed error for the production card-validation test."""

    def __init__(self, message: str, *, status: int):
        super().__init__(message)
        self.user_message = message
        self.status = status


@dataclass(frozen=True)
class ProdValidationCapability:
    """Verified TEMPORARY capability bound to one payment-session hash."""

    session_token_hash: str
    exp: int
    nonce: str


def configured_test_secret() -> Optional[str]:
    """Return the dedicated TEMPORARY secret, or None if missing/short/reused.

    Never falls back to reconciliation, expiry-cron, or cancellation secrets.
    """
    raw = os.getenv(SECRET_ENV)
    if raw is None:
        return None
    secret = str(raw).strip()
    if len(secret) < MIN_SECRET_LENGTH:
        return None
    for env_name in FORBIDDEN_REUSED_SECRET_ENVS:
        other = os.getenv(env_name)
        if isinstance(other, str) and other.strip() and _same_secret(secret, other.strip()):
            return None
    return secret


def configured_test_email() -> Optional[str]:
    """Lodge-controlled QA email from server-only env. No invented address."""
    raw = os.getenv(EMAIL_ENV)
    if raw is None:
        return None
    email = str(raw).strip()
    if not _EMAIL_RE.match(email):
        return None
    return email


def server_persist_failure(error: str, diag: str) -> dict:
    """Build a server-only persist failure. persist_diag is not public."""
    if not isinstance(diag, str) or diag not in SAFE_PERSIST_DIAG_CODES:
        diag = PERSIST_OTHER
    return {"ok": False, "error": error, "persist_diag": diag}


def safe_persist_diag_code(persisted) -> str:
    """Map a persist result to one allowlisted code. Never copies error text."""
    if not isinstance(persisted, dict):
        return PERSIST_OTHER
    raw = persisted.get("persist_diag")
    if isinstance(raw, str) and raw in SAFE_PERSIST_DIAG_CODES:
        return raw
    return PERSIST_OTHER


def persist_failure_user_message(persisted) -> str:
    """TEMPORARY operator 503. Uses only an allowlisted persist category."""
    return f"{SAFE_UNAVAILABLE} [{safe_persist_diag_code(persisted)}]"


def authorize_form_secret(provided) -> None:
    """Authorize a form-body secret only. Query/Bearer/JSON are ignored.

    Missing or short configured secret -> 503. Wrong provided secret -> 401.
    Never logs the provided value.
    """
    expected = configured_test_secret()
    if expected is None:
        raise ProdValidationTestError(SAFE_UNAVAILABLE, status=503)
    if not isinstance(provided, str) or not provided:
        raise ProdValidationTestError(SAFE_UNAUTHORIZED, status=401)
    if len(provided) != len(expected) or not secrets.compare_digest(provided, expected):
        raise ProdValidationTestError(SAFE_UNAUTHORIZED, status=401)


def require_production_moneris_env() -> None:
    env = (os.getenv("MONERIS_ENV") or "").strip()
    if env != "production":
        raise ProdValidationTestError(SAFE_UNAVAILABLE, status=503)


def require_production_api_and_ht_config() -> None:
    """Fail closed unless production Moneris API + HT config both load.

    Discards the config objects. Never returns them to a browser handler.
    """
    require_production_moneris_env()
    try:
        from payment_api.config import load_config
        from payment_api.errors import PaymentConfigError
        from payment_completion import _payment_api_environ
    except ImportError:
        logger.error("temporary prod card-validation test config import failed")
        raise ProdValidationTestError(SAFE_UNAVAILABLE, status=503) from None
    try:
        config = load_config(_payment_api_environ())
    except PaymentConfigError:
        logger.error("temporary prod card-validation test payment_api config invalid")
        raise ProdValidationTestError(SAFE_UNAVAILABLE, status=503) from None
    if getattr(config, "moneris_env", None) != "production":
        raise ProdValidationTestError(SAFE_UNAVAILABLE, status=503)
    try:
        load_hosted_tokenization_browser_config()
    except HostedTokenizationConfigError:
        logger.error("temporary prod card-validation test HT config invalid")
        raise ProdValidationTestError(SAFE_UNAVAILABLE, status=503) from None


def qa_guest_payload(email: str) -> dict:
    return {
        "first_name": QA_FIRST_NAME,
        "last_name": QA_LAST_NAME,
        "email": email,
        "phone": QA_PHONE,
        "address": QA_ADDRESS,
        "city": QA_CITY,
        "country": QA_COUNTRY,
    }


def select_qa_stay(
    catalog_names,
    validate_itinerary: Callable,
    *,
    today: Optional[date] = None,
):
    """Pick one genuinely available room for one night, 60–90 days ahead.

    Uses existing itinerary validation/availability. Does not insert rows.
    Prefers non-smoking types. Returns None when no safe stay exists.
    """
    if today is None:
        today = datetime.now(timezone.utc).date()
    names = _preferred_room_names(catalog_names)
    if not names:
        return None
    for offset in range(QA_MIN_LEAD_DAYS, QA_MAX_LEAD_DAYS + 1):
        check_in = today + timedelta(days=offset)
        check_out = check_in + timedelta(days=1)
        for name in names:
            rooms_req = [
                {"name": name, "adults": 1, "children": 0, "pets": 0}
            ]
            result, _status = validate_itinerary(check_in, check_out, rooms_req)
            if isinstance(result, dict) and result.get("valid"):
                return check_in, check_out, rooms_req, result
    return None


def mint_capability(
    session_token_hash: str,
    *,
    now: Optional[datetime] = None,
    secret: Optional[str] = None,
) -> str:
    """Mint the TEMPORARY HMAC capability for one payment-session hash.

    Cookie payload is nonce + hash + expiry. The raw payment-session token
    and the test secret are not included.
    """
    if not is_session_token_hash(session_token_hash):
        raise ProdValidationTestError(SAFE_UNAVAILABLE, status=503)
    key = secret if secret is not None else configured_test_secret()
    if key is None:
        raise ProdValidationTestError(SAFE_UNAVAILABLE, status=503)
    nonce = secrets.token_urlsafe(32)
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    exp = int(now.timestamp()) + CAPABILITY_TTL_SECONDS
    mac = _capability_mac(key, nonce, session_token_hash, exp)
    return f"v1.{nonce}.{session_token_hash}.{exp}.{mac}"


def parse_capability(
    cookie_value,
    *,
    now: Optional[datetime] = None,
    secret: Optional[str] = None,
) -> Optional[ProdValidationCapability]:
    """Return the capability or None. Never logs the cookie."""
    key = secret if secret is not None else configured_test_secret()
    if key is None or not isinstance(cookie_value, str) or not cookie_value:
        return None
    parts = cookie_value.split(".")
    if len(parts) != 5:
        return None
    version, nonce, session_hash, exp_raw, mac = parts
    if version != "v1" or not nonce or not is_session_token_hash(session_hash):
        return None
    try:
        exp = int(exp_raw)
    except ValueError:
        return None
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if exp <= int(now.timestamp()):
        return None
    expected = _capability_mac(key, nonce, session_hash, exp)
    if len(mac) != len(expected) or not hmac.compare_digest(mac, expected):
        return None
    return ProdValidationCapability(
        session_token_hash=session_hash,
        exp=exp,
        nonce=nonce,
    )


def apply_capability_cookie(response, cookie_value: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        cookie_value,
        max_age=CAPABILITY_TTL_SECONDS,
        httponly=True,
        secure=True,
        samesite="Strict",
        path=COOKIE_PATH,
    )


def clear_capability_cookie(response) -> None:
    response.set_cookie(
        COOKIE_NAME,
        "",
        max_age=0,
        expires=0,
        httponly=True,
        secure=True,
        samesite="Strict",
        path=COOKIE_PATH,
    )


def capability_allows_complete_payment_page(request) -> bool:
    """True when the TEMPORARY cookie is valid in production.

    Does not authorize /booking, /booker_contact, /final_details, or
    /confirm-booking. Those routes must keep using _booking_funnel_blocked().
    """
    if not _production_env():
        return False
    return parse_capability(request.cookies.get(COOKIE_NAME)) is not None


def capability_matches_payment_session(request, raw_token) -> bool:
    """True when the cookie is valid and bound to this payment-session token."""
    if not _production_env():
        return False
    cap = parse_capability(request.cookies.get(COOKIE_NAME))
    if cap is None:
        return False
    if not isinstance(raw_token, str) or not raw_token:
        return False
    try:
        submitted = hash_payment_session_token(raw_token)
    except ValueError:
        return False
    return secrets.compare_digest(submitted, cap.session_token_hash)


def _production_env() -> bool:
    return (os.getenv("MONERIS_ENV") or "").strip() == "production"


def _preferred_room_names(catalog_names) -> list:
    names = [name for name in catalog_names if isinstance(name, str) and name]
    def sort_key(name: str):
        smoking = "Smoking" in name and "Non-Smoking" not in name
        return (1 if smoking else 0, name)
    return sorted(names, key=sort_key)


def _capability_mac(secret: str, nonce: str, session_token_hash: str, exp: int) -> str:
    body = f"{MAC_CONTEXT}|{nonce}|{session_token_hash}|{exp}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _same_secret(left: str, right: str) -> bool:
    if len(left) != len(right):
        return False
    return secrets.compare_digest(left, right)
