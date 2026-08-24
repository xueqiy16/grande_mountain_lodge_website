import os
import re
import uuid
import hashlib
import secrets
import logging
from collections import Counter
from flask import Flask, render_template, request, jsonify, Response, redirect, make_response
from datetime import datetime, timezone
from dotenv import load_dotenv
from supabase import create_client, Client
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from confirmation import (
    build_calendar_ics,
    fetch_confirmation_from_supabase,
    format_guest_date,
    send_confirmation_email,
    validate_guest_payload,
)
from cancellation_capability import (
    configured_cancellation_token_secret,
    create_placeholder_hash,
)
from payment_session import (
    BookingRpcContractError,
    generate_payment_session_token,
    hash_payment_session_token,
    is_session_token_hash,
    pending_payment_browser_payload,
    server_create_booking_result,
    uses_pending_payment_rpc,
)
from payment_completion import (
    PaymentCompletionError,
    complete_pending_payment,
    parse_browser_payment_request,
    payment_completion_error_body,
    require_pending_v7_contract,
)
from payment_ht import (
    HostedTokenizationConfigError,
    load_hosted_tokenization_browser_config,
)
from payment_expiry import (
    PaymentExpiryError,
    authorize_expiry_cron,
    expiry_error_body,
    require_pending_v7_for_expiry,
    run_expire_abandoned_payment_sessions,
)
from payment_reconciliation import (
    PaymentReconciliationError,
    authorize_reconciliation_admin,
    finalize_held_payment,
    list_held_payment_registrations,
    reconciliation_error_body,
    release_held_payment_confirmed_failure,
    require_pending_v7_for_reconciliation,
)
from payment_prod_validation_test import (
    COOKIE_NAME as PROD_VALIDATION_TEST_COOKIE,
    PERSIST_ASSIGN_FAILED,
    PERSIST_GUEST_EMAIL_CONFLICT,
    PERSIST_NO_BOOKING_REFERENCE,
    PERSIST_OTHER,
    PERSIST_RESERVATION_EXPIRED,
    PERSIST_ROOM_UNAVAILABLE,
    PERSIST_RPC_GENERIC,
    PERSIST_STALE_PROCESSING,
    PERSIST_STATE_INCONSISTENT,
    PRE_BOOKING_REF,
    PRE_BOOKINGS_NOT_PAUSED,
    PRE_CANCEL_SECRET,
    PRE_CONTRACT_INVALID,
    PRE_CONTRACT_NOT_PENDING_V7,
    PRE_EMAIL,
    PRE_GUEST,
    PRE_STAY,
    PRE_SUPABASE,
    ProdValidationTestError,
    START_PATH as PROD_VALIDATION_TEST_PATH,
    QA_SPECIAL_REQUESTS,
    apply_capability_cookie,
    authorize_form_secret,
    capability_allows_complete_payment_page,
    capability_matches_payment_session,
    clear_capability_cookie,
    configured_test_email,
    mint_capability,
    persist_failure_user_message,
    preflight_unavailable_message,
    qa_guest_payload,
    require_production_api_and_ht_config,
    safe_persist_diag_code,
    select_qa_stay,
    server_persist_failure,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SUPABASE CLIENT — canonical source of truth for guest bookings
# ---------------------------------------------------------------------------
# The Flask backend is a trusted server-side component and must use a
# server-only Supabase credential. Prefer the newer "Secret key" format, then
# the legacy service-role key. `SUPABASE_KEY` remains as a transitional
# fallback so a deploy does not break before the Vercel env var is renamed.
# This value is NEVER passed to templates, JS, or logs.
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = (
    os.getenv("SUPABASE_SECRET_KEY")
    or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    or os.getenv("SUPABASE_KEY")
)

supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:  # noqa: BLE001
        logger.error("[Supabase] client init failed: %s", e)
        supabase = None
else:
    logger.warning(
        "[Supabase] SUPABASE_URL / SUPABASE_SECRET_KEY not set — bookings disabled."
    )

app = Flask(__name__)

# Temporary pause of the direct booking funnel. Set to False to restore
# /booking, /booker_contact, /final_details, and booking submission.
DIRECT_BOOKINGS_PAUSED = True
PAUSED_BOOKING_ERROR = (
    "Direct online bookings are temporarily disabled for maintenance. "
    "Please book via Booking.com or Expedia."
)


def _booking_funnel_blocked():
    """Booking funnel is blocked whenever the pause flag is set."""
    return DIRECT_BOOKINGS_PAUSED


# create_public_booking contract. live_v6 is the current production 6-arg
# function (confirmed, no payment session). pending_v7 is sql/003's 7-arg
# function. Default live_v6 so this app cannot call a function that does not
# exist until 003 is applied. Cutover is explicit: apply 003, set
# CREATE_PUBLIC_BOOKING_CONTRACT=pending_v7, keep bookings paused until the
# expire caller is deployed and an external ~5-minute scheduler is active.
# Never auto-fallback to 6-arg after a 7-arg failure:
# 6-arg confirms the stay without a payment session.


# ---------------------------------------------------------------------------
# RATE LIMITING
# ---------------------------------------------------------------------------
# Public endpoints are protected with conservative, route-specific limits so
# ordinary human booking is unaffected but scripted abuse (PII enumeration,
# booking spam) is throttled. Static/marketing pages are NOT limited.
#
# Client IP behind the Vercel proxy: Vercel sets `X-Forwarded-For` with the
# real client IP as the first entry. We take that first hop and fall back to
# the socket address. This is the correct trusted source on Vercel; do not
# trust the whole header blindly.
def _client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return get_remote_address() or "127.0.0.1"


limiter = Limiter(
    key_func=_client_ip,
    app=app,
    default_limits=[],           # no blanket limit; only decorated routes
    storage_uri="memory://",     # see deploy notes: per-instance on serverless
    strategy="fixed-window",
    headers_enabled=True,
)


@app.errorhandler(429)
def _rate_limited(err):
    """Uniform 429 for both JSON APIs and page routes."""
    if request.path.startswith("/api/") or request.is_json:
        return jsonify({
            "success": False,
            "valid": False,
            "error": "Too many requests. Please slow down and try again shortly.",
        }), 429
    return (
        "<h3>Too many requests. Please wait a moment and try again.</h3>",
        429,
    )

# All rooms
"""
LEGEND - ROOM CLASSIFICATIONS:
STD: Standard - One room, no kitchen
STU: Studio   - One room + kitchenette
STE: Suite    - Separate bedroom OR open-plan premium
"""

rooms = [
    # Room Number, Specific Name, Room Type
    #Standard Queen Non-Smoking (1 Room)
    {"no": "225", "name": "Standard Queen Non-Smoking", "type": "STD-Q-NS"},
    
    # Studio Queen Non-Smoking (11 Rooms)
    {"no": "105", "name": "Studio Queen Non-Smoking", "type": "STU-Q-NS"},
    {"no": "113", "name": "Studio Queen Non-Smoking", "type": "STU-Q-NS"},
    {"no": "116", "name": "Studio Queen Non-Smoking", "type": "STU-Q-NS"},
    {"no": "122", "name": "Studio Queen Non-Smoking", "type": "STU-Q-NS"},
    {"no": "123", "name": "Studio Queen Non-Smoking", "type": "STU-Q-NS"},
    {"no": "207", "name": "Studio Queen Non-Smoking", "type": "STU-Q-NS"},
    {"no": "210", "name": "Studio Queen Non-Smoking", "type": "STU-Q-NS"},
    {"no": "212", "name": "Studio Queen Non-Smoking", "type": "STU-Q-NS"},
    {"no": "213", "name": "Studio Queen Non-Smoking", "type": "STU-Q-NS"},
    {"no": "219", "name": "Studio Queen Non-Smoking", "type": "STU-Q-NS"},
    {"no": "222", "name": "Studio Queen Non-Smoking", "type": "STU-Q-NS"},

    # Studio Double Queen Non-Smoking (19 Rooms)
    {"no": "101", "name": "Studio Double Queen Non-Smoking", "type": "STU-QQ-NS"},
    {"no": "102", "name": "Studio Double Queen Non-Smoking", "type": "STU-QQ-NS"},
    {"no": "103", "name": "Studio Double Queen Non-Smoking", "type": "STU-QQ-NS"},
    {"no": "108", "name": "Studio Double Queen Non-Smoking", "type": "STU-QQ-NS"},
    {"no": "109", "name": "Studio Double Queen Non-Smoking", "type": "STU-QQ-NS"},
    {"no": "111", "name": "Studio Double Queen Non-Smoking", "type": "STU-QQ-NS"},
    {"no": "112", "name": "Studio Double Queen Non-Smoking", "type": "STU-QQ-NS"},
    {"no": "114", "name": "Studio Double Queen Non-Smoking", "type": "STU-QQ-NS"},
    {"no": "118", "name": "Studio Double Queen Non-Smoking", "type": "STU-QQ-NS"},
    {"no": "120", "name": "Studio Double Queen Non-Smoking", "type": "STU-QQ-NS"},
    {"no": "209", "name": "Studio Double Queen Non-Smoking", "type": "STU-QQ-NS"},
    {"no": "211", "name": "Studio Double Queen Non-Smoking", "type": "STU-QQ-NS"},
    {"no": "214", "name": "Studio Double Queen Non-Smoking", "type": "STU-QQ-NS"},
    {"no": "215", "name": "Studio Double Queen Non-Smoking", "type": "STU-QQ-NS"},
    {"no": "217", "name": "Studio Double Queen Non-Smoking", "type": "STU-QQ-NS"},
    {"no": "218", "name": "Studio Double Queen Non-Smoking", "type": "STU-QQ-NS"},
    {"no": "220", "name": "Studio Double Queen Non-Smoking", "type": "STU-QQ-NS"},
    {"no": "221", "name": "Studio Double Queen Non-Smoking", "type": "STU-QQ-NS"},
    {"no": "223", "name": "Studio Double Queen Non-Smoking", "type": "STU-QQ-NS"},

    # Suite Queen Non-Smoking (2 Rooms)
    {"no": "216", "name": "Suite Queen Non-Smoking", "type": "STE-Q-NS"},
    {"no": "224", "name": "Suite Queen Non-Smoking", "type": "STE-Q-NS"},

    # Suite King Non-Smoking (1 Room)
    {"no": "227", "name": "Suite King Non-Smoking", "type": "STE-K-NS"},

    # Studio Queen Smoking (1 Room)
    {"no": "205", "name": "Studio Queen Smoking", "type": "STU-Q-SM"},

    # Studio Double Queen Smoking (3 Rooms)
    {"no": "202", "name": "Studio Double Queen Smoking", "type": "STU-QQ-SM"},
    {"no": "203", "name": "Studio Double Queen Smoking", "type": "STU-QQ-SM"},
    {"no": "208", "name": "Studio Double Queen Smoking", "type": "STU-QQ-SM"}
]

# Define room prices
ROOM_PRICES = {
    "Classic Queen Smoking": 109.00,
    "Classic Queen Non-Smoking": 119.00,
    "Double Queen Smoking": 139.00,
    "Double Queen Non-Smoking": 149.00
}

# ---------------------------------------------------------------------------
# SERVER-SIDE SOURCE OF TRUTH
# The browser must never be trusted for price, capacity, or availability.
# These values are the authoritative catalog used to re-price and re-verify
# every reservation on submit. Names match the booking page "Book" buttons.
# ---------------------------------------------------------------------------
# Keyed by the exact name the booking page sends via addToCart(...). `code`
# links each entry to the Supabase `room_types` / `rooms` tables. `rate` and
# `total_units` are refreshed from Supabase at runtime when available; the
# values here are the authoritative fallback if Supabase is unreachable.
ROOM_CATALOG = {
    "Standard Queen":                  {"code": "STD-Q-NS",  "capacity": 2, "rate": 84.99,  "total_units": 2},
    "Studio Queen Non-Smoking":        {"code": "STU-Q-NS",  "capacity": 2, "rate": 89.99,  "total_units": 12},
    "Studio Queen Smoking":            {"code": "STU-Q-SM",  "capacity": 2, "rate": 89.99,  "total_units": 1},
    "Suite Queen Non-Smoking":         {"code": "STE-Q-NS",  "capacity": 2, "rate": 104.99, "total_units": 2},
    "Suite King Non-Smoking":          {"code": "STE-K-NS",  "capacity": 2, "rate": 104.99, "total_units": 1},
    "Studio Double Queen Non-Smoking": {"code": "STU-QQ-NS", "capacity": 4, "rate": 99.99,  "total_units": 18},
    "Studio Double Queen Smoking":     {"code": "STU-QQ-SM", "capacity": 4, "rate": 99.99,  "total_units": 3},
}

CODE_TO_NAME = {info["code"]: name for name, info in ROOM_CATALOG.items()}

# Physical inventory counts per room type code, derived from the legacy local
# `rooms` list (fallback only; Supabase `rooms` is queried first at runtime).
INVENTORY = Counter(r["type"] for r in rooms)

# --- Room operational status (public.rooms) ---
# Status values: available, occupied, housekeeping, out-of-service.
# Future reservation eligibility is determined by booking date overlaps — NOT by
# whether a room is currently occupied. Never set status to occupied when a
# future booking is created. Only housekeeping and out-of-service rooms are
# excluded from assignment.
NON_BOOKABLE_ROOM_STATUSES = frozenset({"housekeeping", "house-keeping", "out-of-service"})

# Minimum check-in age enforced in copy only; booking rules in _validate_itinerary.
GST_RATE = 0.05           # Goods & Services Tax
ATL_RATE = 0.06           # Alberta Tourism Levy
ATL_EXEMPT_NIGHTS = 28    # stays of 28+ consecutive nights are ATL-exempt

# Hard limits enforced on every payload
MAX_ROOMS_PER_TXN = 5
MAX_HUMANS_PER_ROOM = 4
MAX_PETS_PER_ROOM = 5

# --- Availability request bounds (server-enforced; never trust the browser) ---
# ASSUMPTION: the repository defined no explicit max-stay / booking-horizon
# policy, so these conservative defaults are introduced. They are intentionally
# generous enough to allow the 28+ night ("monthly") stays referenced by
# ATL_EXEMPT_NIGHTS. Adjust here if the lodge's real policy differs.
MAX_STAY_NIGHTS = 90          # longest single reservation window allowed
MAX_FUTURE_DAYS = 540         # furthest-out check-in date allowed (~18 months)


def _to_non_negative_int(value):
    """Return a non-negative integer, or None if the value is invalid
    (negative, non-numeric, or fractional)."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    i = int(f)
    if i != f or i < 0:
        return None
    return i


def _parse_date(s):
    """Parse the date formats the app may send (ISO, or flatpickr 'j M Y')."""
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%j %b %Y"):
        try:
            return datetime.strptime(str(s).strip(), fmt).date()
        except (TypeError, ValueError):
            continue
    return None


def _compute_taxes(subtotal, nights):
    """GST (always) + Alberta Tourism Levy (waived for 28+ night stays)."""
    gst = round(subtotal * GST_RATE, 2)
    atl = round(subtotal * ATL_RATE, 2) if nights < ATL_EXEMPT_NIGHTS else 0.0
    return gst, atl


def _validate_stay_window(check_in, check_out, today=None):
    """Server-side bounds for a requested stay. Returns (ok, error|None).

    Enforces: valid dates, no past check-in, positive length, a finite maximum
    stay length, and a finite future booking horizon. Pure/stateless so it can
    be unit-tested without Supabase."""
    if today is None:
        today = datetime.now().date()
    if not check_in or not check_out:
        return False, "Invalid check-in/check-out dates."
    if check_in < today:
        return False, "Check-in date cannot be in the past."
    if check_in >= check_out:
        return False, "Check-out must be after check-in."
    if (check_out - check_in).days > MAX_STAY_NIGHTS:
        return False, f"Stays longer than {MAX_STAY_NIGHTS} nights cannot be booked online."
    if (check_in - today).days > MAX_FUTURE_DAYS:
        return False, "That check-in date is too far in the future to book online."
    return True, None


def _verify_token(stored, provided):
    """Constant-time comparison of a confirmation access token.

    Returns False when either side is missing so absence never grants access."""
    if not stored or not provided:
        return False
    return secrets.compare_digest(str(stored), str(provided))


_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9_\-]{16,128}$")


def _normalize_idempotency_key(value):
    """Return a safe idempotency key from client input, or mint a server-side
    UUID when absent/invalid. Format-restricted to avoid unexpected payloads
    reaching the database."""
    if isinstance(value, str) and _IDEMPOTENCY_RE.match(value.strip()):
        return value.strip()
    return uuid.uuid4().hex


def _hash_cancellation_token(token):
    """SHA-256 hex digest of the raw cancellation token. Empty/missing -> ''."""
    if not isinstance(token, str) or not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _create_public_booking_rpc_args(
    idempotency_key,
    booking_ref,
    confirmation_token,
    guest_payload,
    booking_rows,
    cancellation_token_hash,
    session_token_hash=None,
):
    """Named arguments for create_public_booking. 6-arg live vs 7-arg pending."""
    args = {
        "p_idempotency_key": idempotency_key,
        "p_booking_reference": booking_ref,
        "p_confirmation_token": confirmation_token,
        "p_guest": guest_payload,
        "p_bookings": booking_rows,
        "p_cancellation_token_hash": cancellation_token_hash,
    }
    if uses_pending_payment_rpc():
        args["p_session_token_hash"] = session_token_hash
    return args


def _rpc_payload(res):
    """Normalize PostgREST RPC .data which may be a dict or a one-row list."""
    data = getattr(res, "data", None)
    if isinstance(data, list):
        return data[0] if data else {}
    return data or {}


def _public_site_url():
    return (os.getenv("PUBLIC_SITE_URL") or "https://grandemountainlodge.com").rstrip("/")


def _cancel_reservation_url(booking_ref, token):
    return f"{_public_site_url()}/cancel-reservation/{booking_ref}?token={token}"


def _parse_timestamptz(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


_INVALID_CANCEL_MSG = (
    "This cancellation link is invalid, has expired, or has already been used. "
    "If you still need help, please call the lodge at 780-827-2007."
)


def _preview_public_cancellation(booking_ref, token):
    """Read-only check of a guest cancellation link. Never mutates rows.

    Returns a small dict (no guest contact PII) or None when the link is
    invalid / expired / already used / mismatched.
    """
    if not supabase or not booking_ref or not token:
        return None
    token_hash = _hash_cancellation_token(token)
    if len(token_hash) != 64:
        return None
    ref = booking_ref.strip().upper()

    try:
        cancel_rows = (
            supabase.table("cancellation")
            .select("id, booking_id, token_expiry, token_usage, cancellation_token_hash")
            .eq("cancellation_token_hash", token_hash)
            .limit(1)
            .execute()
            .data
        ) or []
    except Exception:  # noqa: BLE001
        logger.error("cancellation preview lookup failed")
        return None
    if not cancel_rows:
        return None

    crow = cancel_rows[0]
    if crow.get("token_usage") is True:
        return None
    expiry = _parse_timestamptz(crow.get("token_expiry"))
    if expiry is None:
        return None
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if expiry <= datetime.now(timezone.utc):
        return None

    try:
        book_rows = (
            supabase.table("bookings")
            .select(
                "booking_id, booking_reference, check_in, check_out, "
                "booking_status, total_nights, "
                "rooms(room_number, room_types!rooms_room_type_id_fkey(name))"
            )
            .eq("booking_reference", ref)
            .execute()
            .data
        ) or []
    except Exception:  # noqa: BLE001
        try:
            book_rows = (
                supabase.table("bookings")
                .select(
                    "booking_id, booking_reference, check_in, check_out, "
                    "booking_status, total_nights"
                )
                .eq("booking_reference", ref)
                .execute()
                .data
            ) or []
        except Exception:  # noqa: BLE001
            logger.error("cancellation preview booking lookup failed")
            return None

    if not book_rows:
        return None
    ids = {row.get("booking_id") for row in book_rows}
    if crow.get("booking_id") not in ids:
        return None

    statuses = {row.get("booking_status") for row in book_rows}
    if "checked_in" in statuses:
        state = "cannot_cancel"
    elif statuses and statuses.issubset({"cancelled", "no_show", "checked_out"}):
        state = "already_cancelled"
    else:
        state = "cancellable"

    rooms = []
    for row in book_rows:
        room = row.get("rooms") or {}
        room_type = room.get("room_types") or {}
        rooms.append({"room_type_name": room_type.get("name") or room.get("code") or "Room"})

    primary = book_rows[0]
    nights = primary.get("total_nights")
    return {
        "state": state,
        "booking_reference": ref,
        "token": token,
        "check_in": format_guest_date(primary.get("check_in")),
        "check_out": format_guest_date(primary.get("check_out")),
        "nights": nights,
        "rooms": rooms,
    }


# --- Supabase-backed catalog / inventory (cached for the process lifetime) ---
_ROOM_TYPES_CACHE = None
_INVENTORY_CACHE = None


def _supabase_required():
    """Return (ok, error_message) — bookings require a configured Supabase client."""
    if not supabase:
        return False, (
            "Our booking system is temporarily unavailable. "
            "Please try again shortly or call 780-827-2007."
        )
    return True, None


def _room_types_by_code():
    """{code: {room_type_id, name, rate}} from Supabase.

    When Supabase is configured, this must load real room_type_id values from the
    database. The static ROOM_CATALOG fallback is only used when Supabase is not
    configured at all (local template development without credentials).
    """
    global _ROOM_TYPES_CACHE
    if _ROOM_TYPES_CACHE is not None:
        return _ROOM_TYPES_CACHE
    data = {}
    if supabase:
        try:
            rows = supabase.table("room_types").select(
                "room_type_id,name,code,nightly_rate").execute().data or []
            for r in rows:
                code = r.get("code")
                if not code:
                    continue
                data[code] = {
                    "room_type_id": r["room_type_id"],
                    "name": r["name"],
                    "rate": float(r["nightly_rate"]),
                }
            if not data:
                logger.error(
                    "[Supabase] room_types returned 0 rows. "
                    "Ensure SUPABASE_KEY is the service-role (secret) key, not the "
                    "publishable/anon key, and that room_types is populated."
                )
        except Exception as e:  # noqa: BLE001
            logger.error("[Supabase] room_types fetch failed: %s", e)
    if not data and not supabase:
        for name, info in ROOM_CATALOG.items():
            data[info["code"]] = {"room_type_id": None, "name": name, "rate": info["rate"]}
    _ROOM_TYPES_CACHE = data
    return data


def _room_type_id_for_code(code):
    """Resolve a room-type code to its Supabase room_type_id."""
    rt = _room_types_by_code().get(code)
    if rt and rt.get("room_type_id"):
        return rt["room_type_id"]
    return None


def _inventory_by_code():
    """{code: total_units} = the STATIC physical base of operationally usable
    rooms per room-type code, counted from Supabase `rooms`.

    This counts every physical room EXCEPT those that are physically unusable
    right now (status 'house-keeping' or 'out-of-service'). Rooms that are
    'available' or 'occupied' are both counted — a currently occupied room is
    still bookable for a non-overlapping future window (calendar overlaps in
    public.bookings decide that, not the momentary room status). Falls back to
    the static totals in ROOM_CATALOG when Supabase is unavailable."""
    global _INVENTORY_CACHE
    if _INVENTORY_CACHE is not None:
        return _INVENTORY_CACHE
    counts = {}
    if supabase:
        try:
            rows = supabase.table("rooms").select(
                "room_id, room_type_id, code, status, room_number"
            ).execute().data
            for r in rows:
                if r.get("status") in NON_BOOKABLE_ROOM_STATUSES:
                    continue
                code = r.get("code")
                if code:
                    counts[code] = counts.get(code, 0) + 1
        except Exception as e:  # noqa: BLE001
            logger.error("[Supabase] rooms inventory fetch failed: %s", e)
    if not counts:
        counts = {info["code"]: info["total_units"] for info in ROOM_CATALOG.values()}
    _INVENTORY_CACHE = counts
    return counts


def _server_rate(code):
    """Authoritative nightly rate for a room-type code (Supabase first)."""
    rt = _room_types_by_code().get(code)
    if rt:
        return rt["rate"]
    name = CODE_TO_NAME.get(code)
    return ROOM_CATALOG.get(name, {}).get("rate", 100.0)


def _overlap_counts_by_code(check_in, check_out):
    """Count overlapping, inventory-blocking bookings per room-type code.

    Half-open overlap: existing_check_in < req_out AND existing_check_out > req_in.
    Cancelled and no-show bookings never block inventory."""
    if not supabase:
        return {}

    ci, co = check_in.isoformat(), check_out.isoformat()
    counts = {}
    try:
        res = (supabase.table("bookings")
               .select("check_in,check_out,booking_status,rooms(code)")
               .lt("check_in", co)
               .gt("check_out", ci)
               .neq("booking_status", "cancelled")
               .neq("booking_status", "no_show")
               .execute())
        for b in res.data:
            room = b.get("rooms") or {}
            code = room.get("code")
            if code:
                counts[code] = counts.get(code, 0) + 1
    except Exception as e:  # noqa: BLE001
        logger.error("[Supabase] overlap query failed: %s", e)
    return counts


def _availability_by_name(check_in, check_out):
    """Remaining units per room name for the requested window (server-verified)."""
    inventory = _inventory_by_code()
    overlaps = _overlap_counts_by_code(check_in, check_out)
    remaining = {}
    for name, info in ROOM_CATALOG.items():
        code = info["code"]
        remaining[name] = max(0, inventory.get(code, 0) - overlaps.get(code, 0))
    return remaining


def _validate_itinerary(check_in, check_out, rooms_req):
    """Sanitize + validate a multi-room itinerary against the Supabase catalog,
    limits, and live availability, then re-price from server rates.
    Returns (result_dict, http_status)."""
    ok, err = _supabase_required()
    if not ok:
        return {"valid": False, "ok": False, "error": err}, 503

    today = datetime.now().date()

    window_ok, window_err = _validate_stay_window(check_in, check_out, today)
    if not window_ok:
        return {"valid": False, "ok": False, "error": window_err}, 400

    if not isinstance(rooms_req, list) or len(rooms_req) == 0:
        return {"valid": False, "ok": False, "error": "No rooms selected."}, 400
    if len(rooms_req) > MAX_ROOMS_PER_TXN:
        return {"valid": False, "ok": False,
                "error": f"A maximum of {MAX_ROOMS_PER_TXN} rooms per booking is allowed."}, 400

    nights = (check_out - check_in).days
    type_counts = {}
    subtotal = 0.0
    verified = []

    for r in rooms_req:
        r = r or {}
        name = r.get("name")
        info = ROOM_CATALOG.get(name)
        # Also accept a room-type code (room_type_id) passthrough
        if not info and r.get("code"):
            name = CODE_TO_NAME.get(r.get("code"))
            info = ROOM_CATALOG.get(name)
        if not info:
            return {"valid": False, "ok": False, "error": f"Unknown room type: {r.get('name')}."}, 400

        adults = _to_non_negative_int(r.get("adults"))
        children = _to_non_negative_int(r.get("children"))
        pets = _to_non_negative_int(r.get("pets", 0))
        if adults is None or children is None or pets is None:
            return {"valid": False, "ok": False,
                    "error": "Guest counts must be non-negative whole numbers."}, 400

        humans = adults + children
        if humans < 1:
            return {"valid": False, "ok": False, "error": "Each room needs at least one guest."}, 400
        if humans > MAX_HUMANS_PER_ROOM or humans > info["capacity"]:
            return {"valid": False, "ok": False,
                    "error": f"{name} allows a maximum of "
                             f"{min(MAX_HUMANS_PER_ROOM, info['capacity'])} guests per room."}, 400
        if pets > MAX_PETS_PER_ROOM:
            return {"valid": False, "ok": False,
                    "error": f"A maximum of {MAX_PETS_PER_ROOM} pets per room is allowed."}, 400

        code = info["code"]
        type_counts[code] = type_counts.get(code, 0) + 1
        rate = _server_rate(code)
        line_total = round(rate * nights, 2)
        subtotal += line_total
        verified.append({
            "name": name,
            "code": code,
            "rate": rate,
            "nights": nights,
            "line_total": line_total,
        })

    # Availability: requested qty per type must fit remaining inventory
    inventory = _inventory_by_code()
    overlaps = _overlap_counts_by_code(check_in, check_out)
    for code, qty in type_counts.items():
        remaining = inventory.get(code, 0) - overlaps.get(code, 0)
        if qty > remaining:
            return {"valid": False, "ok": False,
                    "error": "One or more requested rooms are no longer available for these dates."}, 409

    subtotal = round(subtotal, 2)
    gst, atl = _compute_taxes(subtotal, nights)
    grand_total = round(subtotal + gst + atl, 2)

    return {"valid": True, "ok": True, "nights": nights,
            "subtotal": subtotal, "gst": gst, "atl": atl,
            "grand_total": grand_total, "total": grand_total,
            "rooms": verified}, 200

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/travel-guide')
def travel_guide():
    return render_template('travel-guide.html')

@app.route('/booking-paused')
def booking_paused():
    return render_template('booking_paused.html')


@app.route('/booking')
@app.route('/book')
@app.route('/bookings')
def booking():
    if _booking_funnel_blocked():
        return redirect('/booking-paused')
    return render_template('booking.html')

@app.route('/contact')
def contact():
    return render_template('contact.html')

@app.route('/booker_contact')
def booker_contact():
    if _booking_funnel_blocked():
        return redirect('/booking-paused')
    return render_template('booker_contact.html')

@app.route('/api/verify-cart', methods=['POST'])
@limiter.limit("30 per minute")
def verify_cart():
    """Re-price and re-verify a multi-room cart on the server before checkout.
    The client sends only room names + guest counts + dates; price and
    availability are computed here, never trusted from the browser."""
    data = request.get_json(silent=True) or {}
    check_in = _parse_date(data.get('checkin'))
    check_out = _parse_date(data.get('checkout'))
    rooms_req = data.get('rooms')

    result, status = _validate_itinerary(check_in, check_out, rooms_req)
    return jsonify(result), status


@app.route('/api/availability', methods=['POST'])
@limiter.limit("30 per minute")
def availability():
    """Server-verified remaining units per room type for a set of dates.
    Used by the booking page to grey out sold-out rooms after a search."""
    data = request.get_json(silent=True) or {}
    check_in = _parse_date(data.get('checkin'))
    check_out = _parse_date(data.get('checkout'))

    window_ok, window_err = _validate_stay_window(check_in, check_out)
    if not window_ok:
        return jsonify({"valid": False, "error": window_err}), 400

    return jsonify({"valid": True, "available": _availability_by_name(check_in, check_out)}), 200


# ---------------------------------------------------------------------------
# BOOKING PERSISTENCE (Supabase only)
# ---------------------------------------------------------------------------
# Public guest + booking creation is performed by a single Postgres function
# (`create_public_booking`, see supabase/migrations) so the whole reservation
# is written atomically. Notably, that function INSERTS a fresh guest row for
# every public reservation — it never UPDATEs an existing guest matched by
# email — so an unauthenticated caller can no longer overwrite another guest's
# profile just by submitting their email address.
def _available_physical_rooms(room_type_id, check_in, check_out, exclude_room_ids=None):
    """Physical rooms of a room type that are eligible and not booked for overlapping dates.

    Uses room_type_id (not code) to identify the room type. Excludes housekeeping and
    out-of-service rooms. A currently occupied room remains eligible when it has no
    overlapping booking for the requested window."""
    if not room_type_id:
        return []

    exclude_room_ids = exclude_room_ids or set()
    ci, co = check_in.isoformat(), check_out.isoformat()

    rows = (
        supabase.table("rooms")
        .select("room_id, room_number, status, room_type_id, code")
        .eq("room_type_id", room_type_id)
        .execute()
        .data
    )
    eligible = [
        r for r in rows
        if r.get("status") not in NON_BOOKABLE_ROOM_STATUSES
        and r["room_id"] not in exclude_room_ids
    ]
    if not eligible:
        return []

    room_ids = [r["room_id"] for r in eligible]
    busy = (
        supabase.table("bookings")
        .select("room_id")
        .in_("room_id", room_ids)
        .lt("check_in", co)
        .gt("check_out", ci)
        .neq("booking_status", "cancelled")
        .neq("booking_status", "no_show")
        .execute()
        .data
    )
    busy_ids = {b["room_id"] for b in busy}
    return [r for r in eligible if r["room_id"] not in busy_ids]


def _assign_physical_rooms(check_in, check_out, result):
    """Pre-select one physical room per requested room type before any DB writes."""
    assigned_ids = set()
    assignments = []

    for vr in result["rooms"]:
        room_type_id = _room_type_id_for_code(vr["code"])
        if not room_type_id:
            return None, (
                f"Room type {vr.get('name')} is not configured in the database. "
                "If Supabase credentials are set, verify SUPABASE_KEY is the "
                "service-role (secret) key from Project Settings → API, not the "
                "publishable key."
            )

        available = _available_physical_rooms(
            room_type_id, check_in, check_out, exclude_room_ids=assigned_ids
        )
        if not available:
            return None, "One or more requested rooms are no longer available for these dates."

        chosen = available[0]
        assigned_ids.add(chosen["room_id"])
        assignments.append({
            **vr,
            "room_id": chosen["room_id"],
            "room_number": chosen.get("room_number"),
            "room_type_id": room_type_id,
        })

    return assignments, None


def _generate_booking_reference():
    """Guest-facing reference BK-XXXXXX; regenerate on unlikely collision."""
    for _ in range(5):
        ref = "BK-" + secrets.token_hex(3).upper()
        existing = (
            supabase.table("bookings")
            .select("booking_id")
            .eq("booking_reference", ref)
            .limit(1)
            .execute()
            .data
        )
        if not existing:
            return ref
    raise RuntimeError("Could not generate a unique booking reference.")


def _persist_booking(check_in, check_out, result, rooms_req, guest,
                     special_requests, booking_ref, confirmation_token,
                     idempotency_key, cancellation_token_hash,
                     session_token_hash=None):
    """Create the guest + all booking rows atomically via a Postgres RPC.

    Prices, taxes, booking_status and amount_paid are computed here (server
    side) and passed to the DB; the RPC never trusts client pricing. The RPC:
      * is idempotent on `idempotency_key` (a repeat submit returns the
        original reservation instead of creating a duplicate),
      * INSERTs a new guest (never overwrites one by email),
      * re-checks room availability under a per-room lock inside the same
        transaction and rolls everything back if a room was just taken,
      * inserts the hashed cancellation credential in the same transaction
        so a booking is never stored without a cancel token.

    live_v6 omits p_session_token_hash. pending_v7 requires the SHA-256 of
    the browser payment-session token (never the raw token).

    Returns a server dict. Callers must filter before any browser response.
    """
    ci, co = check_in.isoformat(), check_out.isoformat()
    nights = result["nights"]
    note = (special_requests or "").strip()[:1000] or None

    assignments, assign_err = _assign_physical_rooms(check_in, check_out, result)
    if assign_err:
        return server_persist_failure(assign_err, PERSIST_ASSIGN_FAILED)

    booking_rows = []
    for idx, assignment in enumerate(assignments):
        req = rooms_req[idx] if idx < len(rooms_req) else {}
        sub = assignment["line_total"]
        gst, atl = _compute_taxes(sub, nights)
        line_grand = round(sub + gst + atl, 2)
        booking_rows.append({
            "room_id": assignment["room_id"],
            "check_in": ci,
            "check_out": co,
            "adults": _to_non_negative_int((req or {}).get("adults")) or 1,
            "children": _to_non_negative_int((req or {}).get("children")) or 0,
            "pets": _to_non_negative_int((req or {}).get("pets")) or 0,
            "room_price": assignment["rate"],
            "total_nights": nights,
            "total_price": line_grand,
            "booking_notes": note,
        })

    guest_payload = {
        "first_name": guest["first_name"],
        "last_name": guest["last_name"],
        "email": guest["email"],
        "phone": guest["phone"],
        "address": guest["address"],
        "city": guest["city"],
        "country": guest["country"],
    }

    try:
        pending_rpc = uses_pending_payment_rpc()
    except BookingRpcContractError:
        logger.error("invalid CREATE_PUBLIC_BOOKING_CONTRACT")
        return server_persist_failure(
            "Online booking is temporarily unavailable.",
            PERSIST_OTHER,
        )
    if pending_rpc:
        if not is_session_token_hash(session_token_hash):
            return server_persist_failure(
                "Could not store your booking. Please try again.",
                PERSIST_OTHER,
            )

    try:
        res = supabase.rpc(
            "create_public_booking",
            _create_public_booking_rpc_args(
                idempotency_key,
                booking_ref,
                confirmation_token,
                guest_payload,
                booking_rows,
                cancellation_token_hash,
                session_token_hash=session_token_hash,
            ),
        ).execute()
    except Exception as exc:  # noqa: BLE001
        # Identifier mapping still uses str(exc) locally and is not logged.
        logger.error(
            "create_public_booking RPC failed: type=%s",
            type(exc).__name__,
        )
        message = str(exc)
        if "room_unavailable" in message:
            return server_persist_failure(
                "One or more requested rooms are no longer available for these dates.",
                PERSIST_ROOM_UNAVAILABLE,
            )
        if "guest_email_conflict" in message:
            logger.error(
                "guests.email appears to be UNIQUE — insert-only guest creation "
                "is blocked. Booking refused to avoid overwriting guest PII."
            )
            return server_persist_failure(
                "We couldn't complete your booking online. Please call the lodge to reserve.",
                PERSIST_GUEST_EMAIL_CONFLICT,
            )
        if "reservation_expired" in message:
            return server_persist_failure(
                "This reservation hold has expired. Please start a new booking.",
                PERSIST_RESERVATION_EXPIRED,
            )
        if "payment_session_stale_processing" in message:
            return server_persist_failure(
                "Payment is still being processed. Please wait a moment and try again.",
                PERSIST_STALE_PROCESSING,
            )
        if "reservation_state_inconsistent" in message:
            return server_persist_failure(
                "We couldn't complete your booking online. Please call the lodge to reserve.",
                PERSIST_STATE_INCONSISTENT,
            )
        return server_persist_failure(
            "Could not store your booking. Please try again.",
            PERSIST_RPC_GENERIC,
        )

    data = _rpc_payload(res)
    if not data.get("booking_reference"):
        logger.error("create_public_booking returned no booking_reference")
        return server_persist_failure(
            "Could not store your booking. Please try again.",
            PERSIST_NO_BOOKING_REFERENCE,
        )

    return server_create_booking_result(data)


@app.route('/confirm-booking', methods=['POST'])
@app.route('/api/complete-booking', methods=['POST'])
@limiter.limit("6 per minute")
def handle_booking():
    """Finalize a reservation in Supabase. Re-runs validation immediately before write."""
    if _booking_funnel_blocked():
        return jsonify({"error": PAUSED_BOOKING_ERROR}), 403

    try:
        pending_rpc = uses_pending_payment_rpc()
    except BookingRpcContractError:
        logger.error("invalid CREATE_PUBLIC_BOOKING_CONTRACT")
        return jsonify({
            "success": False,
            "error": "Online booking is temporarily unavailable.",
        }), 503

    cancel_secret = None
    if pending_rpc:
        cancel_secret = configured_cancellation_token_secret()
        if cancel_secret is None:
            logger.error("cancellation capability secret is missing or too short")
            return jsonify({
                "success": False,
                "error": "Online booking is temporarily unavailable.",
            }), 503

    ok, err = _supabase_required()
    if not ok:
        return jsonify({"success": False, "error": err}), 503

    data = request.get_json(silent=True)
    if data is None:
        return _legacy_form_booking()

    guest_ok, guest_result = validate_guest_payload(data.get("guest") or {})
    if not guest_ok:
        return jsonify({"success": False, "error": guest_result}), 400
    guest = guest_result

    check_in = _parse_date(data.get('checkin'))
    check_out = _parse_date(data.get('checkout'))
    rooms_req = data.get('rooms')

    result, status = _validate_itinerary(check_in, check_out, rooms_req)
    if not result.get("valid"):
        return jsonify({"success": False, "error": result.get("error")}), status

    # Idempotency: same key => same reservation, even on double-click/retry.
    idempotency_key = _normalize_idempotency_key(data.get("idempotency_key"))

    try:
        booking_ref = _generate_booking_reference()
    except RuntimeError as exc:
        logger.error(str(exc))
        return jsonify({"success": False, "error": "Could not create booking reference."}), 500

    # High-entropy confirmation access token (never logged, never an internal ID).
    confirmation_token = secrets.token_urlsafe(32)
    # live_v6: random token stays in this request for the email link.
    # pending_v7: store a re-derivable placeholder hash only. The raw
    # token is discarded; email later replaces this hash with the
    # reservation_id HMAC capability.
    cancellation_token = None
    if pending_rpc:
        cancellation_token_hash = create_placeholder_hash(cancel_secret, booking_ref)
    else:
        cancellation_token = secrets.token_urlsafe(32)
        cancellation_token_hash = _hash_cancellation_token(cancellation_token)

    payment_session_token = None
    payment_session_token_hash = None
    if pending_rpc:
        payment_session_token = generate_payment_session_token()
        payment_session_token_hash = hash_payment_session_token(payment_session_token)

    persisted = _persist_booking(
        check_in, check_out, result, rooms_req,
        guest, data.get('special_requests'), booking_ref,
        confirmation_token, idempotency_key, cancellation_token_hash,
        session_token_hash=payment_session_token_hash)

    if not persisted.get("ok"):
        return jsonify({
            "success": False,
            "error": persisted.get("error", "Could not store your booking."),
        }), 500

    # On an idempotent replay the RPC returns the ORIGINAL reference + token.
    # Use `or` (not get's default) so a null/absent value in the RPC echo can
    # never wipe the server-generated token we already persisted.
    booking_ref = persisted.get("booking_reference") or booking_ref
    confirmation_token = persisted.get("confirmation_token") or confirmation_token
    reused = persisted.get("reused", False)

    if pending_rpc:
        # Email and the public confirmation page wait until credential
        # registration + finalize succeed. Do not put confirmation_token in
        # the browser payload or redirect URL.
        browser = pending_payment_browser_payload(
            {
                "ok": True,
                "booking_reference": booking_ref,
                "reused": reused,
                "token_rotated": persisted.get("token_rotated"),
            },
            raw_payment_session_token=payment_session_token,
            nights=result["nights"],
            subtotal=result["subtotal"],
            gst=result["gst"],
            atl=result["atl"],
            grand_total=result["grand_total"],
        )
        return jsonify(browser), 200

    confirmation = fetch_confirmation_from_supabase(
        supabase, booking_ref, token=confirmation_token)

    if confirmation and cancellation_token:
        confirmation["cancel_url"] = _cancel_reservation_url(
            booking_ref, cancellation_token)

    # Only send the confirmation email on the first successful creation, so a
    # retry/refresh does not re-email the guest. On a replay we report success
    # so the guest is not shown a misleading "email failed" notice.
    if reused:
        email_sent, email_error = True, None
    else:
        email_sent, email_error = send_confirmation_email(app, confirmation)

    redirect_url = f"/reservation-confirmation/{booking_ref}?token={confirmation_token}"

    return jsonify({
        "success": True,
        "booking_reference": booking_ref,
        "redirect_url": redirect_url,
        "nights": result["nights"],
        "subtotal": result["subtotal"],
        "gst": result["gst"],
        "atl": result["atl"],
        "grand_total": result["grand_total"],
        "email_sent": email_sent,
        "email_error": email_error if not email_sent else None,
    }), 200


def _legacy_form_booking():
    """Original single-room form handler (used only for non-JSON posts)."""
    ok, err = _supabase_required()
    if not ok:
        return f"<h3>{err}</h3>", 503

    room = request.form.get('room_selection')
    start_str = request.form.get('start_date')
    end_str = request.form.get('end_date')

    if not start_str or not end_str:
        return "<h3>Error: Please go back and select check-in and check-out dates.</h3>", 400

    adults = _to_non_negative_int(request.form.get('adult_count'))
    kids = _to_non_negative_int(request.form.get('child_count'))
    pets = _to_non_negative_int(request.form.get('pet_count'))
    if adults is None or kids is None or pets is None:
        return "<h3>Error: Guest counts must be non-negative whole numbers.</h3>", 400

    start_date = _parse_date(start_str)
    end_date = _parse_date(end_str)
    if not start_date or not end_date:
        return "<h3>Error: Invalid date format.</h3>", 400

    result, status = _validate_itinerary(start_date, end_date, [{
        "name": room, "adults": adults, "children": kids, "pets": pets,
    }])
    if not result.get("valid"):
        return f"<h3>Error: {result.get('error')}</h3>", status

    # This legacy non-JSON form does not collect the guest identity (name,
    # email, address) that a reservation requires, so it cannot safely create a
    # guest + booking. Direct the visitor to the standard online flow instead
    # of writing an incomplete/blank guest record.
    return (
        "<h3>Please complete your reservation through our online booking page.</h3>"
        "<p><a href='/booking'>Return to booking</a></p>"
    ), 400

# NOTE: The legacy prototype routes `/admin-dashboard` and
# `/delete-booking/<id>` were removed. They operated on a local SQLite file
# with NO authentication, which allowed any anonymous visitor to view prototype
# data and delete rows. Real booking management is handled by the separate
# authenticated LodgeOS admin, so these public routes are gone entirely.


@app.route('/elements.html')
def elements():
    return render_template('elements.html')

@app.route('/generic.html')
def generic():
    return render_template('generic.html')

@app.route('/rooms')
def rooms():
    # This renders the rooms.html file located in your templates folder
    return render_template('rooms.html')

@app.route('/final_details')
def final_details():
    if _booking_funnel_blocked():
        return redirect('/booking-paused')
    return render_template('final_details.html')


@app.route('/complete-payment')
def complete_payment():
    """Intermediate payment step after pending_payment booking create.

    Hosted Tokenization iframe is created only after a payment-session token
    is present in sessionStorage. The raw token must not appear in this URL.
    Page is pending_v7 only.

    TEMPORARY: while DIRECT_BOOKINGS_PAUSED is True, the single QA capability
    minted by /internal/prod-card-validation-test may load this real page.
    Public requests still redirect to /booking-paused. This does not unpause
    /booking, /booker_contact, /final_details, or /confirm-booking.
    """
    if _booking_funnel_blocked() and not capability_allows_complete_payment_page(request):
        return redirect('/booking-paused')
    try:
        enabled = uses_pending_payment_rpc()
    except BookingRpcContractError:
        logger.error("invalid CREATE_PUBLIC_BOOKING_CONTRACT")
        return render_template("complete_payment.html", payment_enabled=False), 503
    if not enabled:
        return render_template("complete_payment.html", payment_enabled=False), 404
    try:
        ht = load_hosted_tokenization_browser_config()
    except HostedTokenizationConfigError:
        logger.error("hosted tokenization config invalid")
        return render_template("complete_payment.html", payment_enabled=False), 503
    response = make_response(
        render_template(
            "complete_payment.html",
            payment_enabled=True,
            ht_iframe_src=ht.iframe_src,
            ht_origin=ht.postmessage_origin,
            ht_token_min_length=ht.token_min_length,
            ht_token_max_length=ht.token_max_length,
        )
    )
    response.headers["Content-Security-Policy"] = f"frame-src {ht.postmessage_origin}"
    return response


@app.route('/api/complete-payment', methods=['POST'])
@limiter.limit("6 per minute")
def handle_complete_payment():
    """Claim the payment session, register the credential, then finalize.

    Browser may send only payment_session_token + dataKey. Unavailable on
    live_v6. Claim commits before any Moneris HTTP.

    TEMPORARY: while paused, accept only when the QA capability is valid
    AND bound to the submitted payment_session_token. Otherwise reject
    before claim/Moneris. Success and any PaymentCompletionError with
    retry_payment != True clear the TEMPORARY capability cookie.
    retry_payment=true keeps the same cookie for the same session.
    """
    payload = request.get_json(silent=True)
    if _booking_funnel_blocked():
        raw_token = payload.get("payment_session_token") if isinstance(payload, dict) else None
        if not capability_matches_payment_session(request, raw_token):
            return jsonify({"success": False, "error": PAUSED_BOOKING_ERROR}), 403

    try:
        require_pending_v7_contract()
    except PaymentCompletionError as exc:
        return _temporary_prod_validation_completion_response(exc)

    ok, err = _supabase_required()
    if not ok:
        return jsonify({"success": False, "error": err}), 503

    try:
        token, data_key = parse_browser_payment_request(payload)
        body = complete_pending_payment(
            payment_session_token=token,
            data_key=data_key,
            supabase=supabase,
            fetch_confirmation=lambda ref, tok: fetch_confirmation_from_supabase(
                supabase, ref, token=tok
            ),
            send_email=lambda confirmation: send_confirmation_email(
                app, confirmation
            ),
        )
    except PaymentCompletionError as exc:
        return _temporary_prod_validation_completion_response(exc)
    response = make_response(jsonify(body), 200)
    if body.get("success") and request.cookies.get(PROD_VALIDATION_TEST_COOKIE):
        clear_capability_cookie(response)
    return response


def _temporary_prod_validation_completion_response(exc: PaymentCompletionError):
    """Preserve completion error JSON; clear TEMPORARY capability unless retryable."""
    response = make_response(jsonify(payment_completion_error_body(exc)), exc.status)
    if not exc.retry_payment and request.cookies.get(PROD_VALIDATION_TEST_COOKIE):
        clear_capability_cookie(response)
    return response


@app.route(PROD_VALIDATION_TEST_PATH, methods=["GET", "POST"])
@limiter.limit("5 per minute")
def prod_card_validation_test():
    """TEMPORARY operator start for one production card-validation attempt.

    GET: password form only. Does not create a reservation or set a capability.
    POST: form-body secret only; creates exactly one QA pending_payment
    reservation through the existing persist path, then hands off to the
    real /complete-payment page. Remove after the authorized test.
    """
    if request.method == "GET":
        return render_template("prod_validation_test_auth.html")

    try:
        authorize_form_secret(request.form.get("secret"))
        if not DIRECT_BOOKINGS_PAUSED:
            logger.error("temporary prod card-validation test refused: PRE_BOOKINGS_NOT_PAUSED")
            raise ProdValidationTestError(
                preflight_unavailable_message(PRE_BOOKINGS_NOT_PAUSED),
                status=503,
            )
        try:
            pending_rpc = uses_pending_payment_rpc()
        except BookingRpcContractError:
            logger.error("temporary prod card-validation test refused: PRE_CONTRACT_INVALID")
            raise ProdValidationTestError(
                preflight_unavailable_message(PRE_CONTRACT_INVALID),
                status=503,
            ) from None
        if not pending_rpc:
            logger.error("temporary prod card-validation test refused: PRE_CONTRACT_NOT_PENDING_V7")
            raise ProdValidationTestError(
                preflight_unavailable_message(PRE_CONTRACT_NOT_PENDING_V7),
                status=503,
            )
        require_production_api_and_ht_config()
        email = configured_test_email()
        if email is None:
            logger.error("temporary prod card-validation test refused: PRE_EMAIL")
            raise ProdValidationTestError(
                preflight_unavailable_message(PRE_EMAIL),
                status=503,
            )
        cancel_secret = configured_cancellation_token_secret()
        if cancel_secret is None:
            logger.error("temporary prod card-validation test refused: PRE_CANCEL_SECRET")
            raise ProdValidationTestError(
                preflight_unavailable_message(PRE_CANCEL_SECRET),
                status=503,
            )
        ok, _err = _supabase_required()
        if not ok:
            logger.error("temporary prod card-validation test refused: PRE_SUPABASE")
            raise ProdValidationTestError(
                preflight_unavailable_message(PRE_SUPABASE),
                status=503,
            )
        guest_ok, guest_result = validate_guest_payload(qa_guest_payload(email))
        if not guest_ok:
            logger.error("temporary prod card-validation test refused: PRE_GUEST")
            raise ProdValidationTestError(
                preflight_unavailable_message(PRE_GUEST),
                status=503,
            )
        stay = select_qa_stay(ROOM_CATALOG.keys(), _validate_itinerary)
        if stay is None:
            logger.error("temporary prod card-validation test refused: PRE_STAY")
            raise ProdValidationTestError(
                preflight_unavailable_message(PRE_STAY),
                status=503,
            )
        check_in, check_out, rooms_req, result = stay
        try:
            booking_ref = _generate_booking_reference()
        except RuntimeError:
            logger.error("temporary prod card-validation test refused: PRE_BOOKING_REF")
            raise ProdValidationTestError(
                preflight_unavailable_message(PRE_BOOKING_REF),
                status=503,
            ) from None
        confirmation_token = secrets.token_urlsafe(32)
        cancellation_token_hash = create_placeholder_hash(cancel_secret, booking_ref)
        payment_session_token = generate_payment_session_token()
        payment_session_token_hash = hash_payment_session_token(payment_session_token)
        persisted = _persist_booking(
            check_in, check_out, result, rooms_req,
            guest_result, QA_SPECIAL_REQUESTS, booking_ref,
            confirmation_token, uuid.uuid4().hex, cancellation_token_hash,
            session_token_hash=payment_session_token_hash)
        if not persisted.get("ok"):
            code = safe_persist_diag_code(persisted)
            logger.error("temporary prod card-validation test persist failed: %s", code)
            raise ProdValidationTestError(
                persist_failure_user_message(persisted),
                status=503,
            )
        capability = mint_capability(payment_session_token_hash)
        response = make_response(
            render_template(
                "prod_validation_test_handoff.html",
                payment_session_token=payment_session_token,
            )
        )
        apply_capability_cookie(response, capability)
        return response
    except ProdValidationTestError as exc:
        return exc.user_message, exc.status


@app.route("/api/internal/expire-payment-sessions", methods=["POST"])
@limiter.limit("30 per minute")
def handle_expire_payment_sessions():
    """External scheduler entry for expire_abandoned_payment_sessions().

    Bearer PAYMENT_EXPIRY_CRON_SECRET only. Ignores request bodies and query
    strings. Does not inspect cards, call Moneris, or cancel rows in Python.
    Available while DIRECT_BOOKINGS_PAUSED is True. Requires pending_v7.
    """
    try:
        authorize_expiry_cron(request.headers.get("Authorization"))
        require_pending_v7_for_expiry()
    except PaymentExpiryError as exc:
        return jsonify(expiry_error_body(exc)), exc.status

    ok, _err = _supabase_required()
    if not ok:
        unavailable = PaymentExpiryError(
            "Payment session expiry is unavailable.", status=503
        )
        return jsonify(expiry_error_body(unavailable)), 503

    try:
        body = run_expire_abandoned_payment_sessions(supabase)
    except PaymentExpiryError as exc:
        return jsonify(expiry_error_body(exc)), exc.status
    return jsonify(body), 200


def _reconciliation_guard():
    authorize_reconciliation_admin(request.headers.get("Authorization"))
    require_pending_v7_for_reconciliation()
    ok, _err = _supabase_required()
    if not ok:
        raise PaymentReconciliationError(
            "Payment reconciliation is unavailable.", status=503
        )


@app.route("/api/internal/payment-reconciliation/held", methods=["GET"])
@limiter.limit("30 per minute")
def handle_list_held_payment_registrations():
    """Read-only held PENDING / RECONCILIATION_REQUIRED list for ops."""
    try:
        _reconciliation_guard()
        body = list_held_payment_registrations(supabase)
    except PaymentReconciliationError as exc:
        return jsonify(reconciliation_error_body(exc)), exc.status
    return jsonify(body), 200


@app.route(
    "/api/internal/payment-reconciliation/<session_id>/finalize",
    methods=["POST"],
)
@limiter.limit("30 per minute")
def handle_finalize_held_payment(session_id):
    """Finalize only when the current attempt is already SUCCEEDED."""
    try:
        _reconciliation_guard()
        body = finalize_held_payment(
            supabase, session_id, request.get_json(silent=True)
        )
    except PaymentReconciliationError as exc:
        return jsonify(reconciliation_error_body(exc)), exc.status
    return jsonify(body), 200


@app.route(
    "/api/internal/payment-reconciliation/<session_id>/release-confirmed-failure",
    methods=["POST"],
)
@limiter.limit("30 per minute")
def handle_release_held_payment(session_id):
    """Release held inventory after a human-confirmed processor failure."""
    try:
        _reconciliation_guard()
        body = release_held_payment_confirmed_failure(
            supabase, session_id, request.get_json(silent=True)
        )
    except PaymentReconciliationError as exc:
        return jsonify(reconciliation_error_body(exc)), exc.status
    return jsonify(body), 200


@app.route('/reservation-confirmation/<booking_ref>')
@limiter.limit("20 per minute")
def reservation_confirmation(booking_ref):
    """Refresh-safe confirmation page. Requires the high-entropy access token so
    guest PII is not exposed by a (potentially guessable) booking reference."""
    ok, err = _supabase_required()
    if not ok:
        return render_template(
            'reservation_confirmation.html',
            confirmation=None,
            error=err,
            email_notice=None,
        ), 503

    token = request.args.get("token")
    confirmation = fetch_confirmation_from_supabase(supabase, booking_ref, token=token)
    if not confirmation:
        return render_template(
            'reservation_confirmation.html',
            confirmation=None,
            error="We could not find that reservation. Please use the secure link from your confirmation, or contact the lodge.",
            email_notice=None,
        ), 404

    email_notice = request.args.get("email_failed") == "1"
    return render_template(
        'reservation_confirmation.html',
        confirmation=confirmation,
        error=None,
        email_notice=email_notice,
    )


@app.route('/reservation-confirmation/<booking_ref>/calendar.ics')
@limiter.limit("20 per minute")
def reservation_calendar(booking_ref):
    """Downloadable calendar event for the confirmed stay. Token-protected."""
    ok, err = _supabase_required()
    if not ok:
        return err, 503

    token = request.args.get("token")
    confirmation = fetch_confirmation_from_supabase(supabase, booking_ref, token=token)
    if not confirmation:
        return "Reservation not found.", 404

    ics = build_calendar_ics(confirmation)
    if not ics:
        return "Calendar data unavailable for this reservation.", 400

    filename = f"grande-mountain-lodge-{booking_ref}.ics"
    return Response(
        ics,
        mimetype="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _render_cancel_invalid(status=404):
    return render_template(
        "cancel_reservation.html",
        state="invalid",
        error=_INVALID_CANCEL_MSG,
        preview=None,
    ), status


@app.route('/cancel-reservation/<booking_ref>', methods=['GET', 'POST'])
@limiter.limit("10 per minute")
def cancel_reservation(booking_ref):
    """Guest self-service cancellation. GET previews; POST performs the cancel.

    The raw token is never logged. GET never mutates booking or token state.
    """
    ok, err = _supabase_required()
    if not ok:
        return render_template(
            "cancel_reservation.html",
            state="invalid",
            error=err,
            preview=None,
        ), 503

    if request.method == "POST":
        token = (request.form.get("token") or request.args.get("token") or "").strip()
    else:
        token = (request.args.get("token") or "").strip()

    if not token:
        return _render_cancel_invalid()

    preview = _preview_public_cancellation(booking_ref, token)
    if not preview:
        return _render_cancel_invalid()

    if request.method == "GET":
        return render_template(
            "cancel_reservation.html",
            state=preview["state"],
            error=None,
            preview=preview,
        )

    # POST: re-validated above; now cancel atomically. Already-cancelled
    # reservations still go through the RPC so the token is consumed.
    if preview["state"] == "cannot_cancel":
        return render_template(
            "cancel_reservation.html",
            state="cannot_cancel",
            error=None,
            preview=preview,
        ), 409

    token_hash = _hash_cancellation_token(token)
    try:
        res = supabase.rpc("cancel_public_booking", {
            "p_booking_reference": preview["booking_reference"],
            "p_cancellation_token_hash": token_hash,
        }).execute()
        data = _rpc_payload(res)
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        logger.error("cancel_public_booking RPC failed: %s", type(exc).__name__)
        if "cannot_cancel" in message:
            return render_template(
                "cancel_reservation.html",
                state="cannot_cancel",
                error=None,
                preview=preview,
            ), 409
        return _render_cancel_invalid()

    if not data.get("ok"):
        return _render_cancel_invalid()

    preview["state"] = "cancelled"
    return render_template(
        "cancel_reservation.html",
        state="cancelled",
        error=None,
        preview=preview,
    )


@app.route('/privacy-policy')
def privacy_policy():
    return render_template('private_policy.html')

@app.route('/terms-and-conditions')
def terms_and_conditions():
    return render_template('terms_and_conditions.html')

if __name__ == '__main__':
    app.run(debug=True, port=5001)