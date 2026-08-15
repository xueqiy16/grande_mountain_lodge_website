import os
import sqlite3
import secrets
import logging
from collections import Counter
from flask import Flask, render_template, request, redirect, jsonify, Response
from datetime import datetime
from dotenv import load_dotenv
from supabase import create_client, Client

from confirmation import (
    build_calendar_ics,
    fetch_confirmation_from_supabase,
    send_confirmation_email,
    validate_guest_payload,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SUPABASE CLIENT — canonical source of truth for guest bookings
# ---------------------------------------------------------------------------
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:  # noqa: BLE001
        logger.error("[Supabase] client init failed: %s", e)
        supabase = None
else:
    logger.warning("[Supabase] SUPABASE_URL / SUPABASE_KEY not set — bookings disabled.")

app = Flask(__name__)

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

    if not check_in or not check_out:
        return {"valid": False, "ok": False, "error": "Invalid check-in/check-out dates."}, 400
    if check_in < today:
        return {"valid": False, "ok": False, "error": "Check-in date cannot be in the past."}, 400
    if check_in >= check_out:
        return {"valid": False, "ok": False, "error": "Check-out must be after check-in."}, 400

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

# Legacy SQLite table used ONLY by the prototype /admin-dashboard route.
# Guest bookings are persisted exclusively in Supabase — not here.
def init_db():
    conn = sqlite3.connect('bookings.db')
    db = conn.cursor()
    db.execute('''
        CREATE TABLE IF NOT EXISTS reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_type TEXT,
            checkin TEXT,
            checkout TEXT,
            adults INTEGER,
            children INTEGER,
            pets INTEGER,
            total_price REAL
        )
    ''')

    # Indexes mirroring the Supabase availability indexes, so the local SQLite
    # fallback (_overlap_counts_sqlite) stays fast during offline testing.
    # Dates are stored ISO ('YYYY-MM-DD'), so lexical order == chronological order
    # and the half-open range scan (checkout > ?, checkin < ?) can use these.
    db.execute('CREATE INDEX IF NOT EXISTS idx_reservations_dates ON reservations (checkout, checkin)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_reservations_room_type_dates ON reservations (room_type, checkout, checkin)')

    conn.commit()
    conn.close()


@app.route('/')
def home():
    return render_template('index.html')

@app.route('/travel-guide')
def travel_guide():
    return render_template('travel-guide.html')

@app.route('/booking')
def booking():
    return render_template('booking.html')

@app.route('/contact')
def contact():
    return render_template('contact.html')

@app.route('/booker_contact')
def booker_contact():
    return render_template('booker_contact.html')

@app.route('/api/verify-cart', methods=['POST'])
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
def availability():
    """Server-verified remaining units per room type for a set of dates.
    Used by the booking page to grey out sold-out rooms after a search."""
    data = request.get_json(silent=True) or {}
    check_in = _parse_date(data.get('checkin'))
    check_out = _parse_date(data.get('checkout'))
    today = datetime.now().date()

    if not check_in or not check_out or check_in >= check_out or check_in < today:
        return jsonify({"valid": False, "error": "Invalid check-in/check-out dates."}), 400

    return jsonify({"valid": True, "available": _availability_by_name(check_in, check_out)}), 200


# ---------------------------------------------------------------------------
# BOOKING PERSISTENCE (Supabase only)
# ---------------------------------------------------------------------------
def _upsert_guest(guest):
    """Find a guest by email (update) or insert a new one. Returns guest_id."""
    email = guest["email"]
    payload = {
        "first_name": guest["first_name"],
        "last_name": guest["last_name"],
        "email": email,
        "phone": guest["phone"],
        "address": guest["address"],
        "city": guest["city"],
        "country": guest["country"],
    }
    found = supabase.table("guests").select("guest_id").eq("email", email).limit(1).execute().data
    if found:
        gid = found[0]["guest_id"]
        supabase.table("guests").update(payload).eq("guest_id", gid).execute()
        return gid
    inserted = supabase.table("guests").insert(payload).execute().data
    return inserted[0]["guest_id"]


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


def _rollback_bookings_by_reference(booking_ref):
    """Best-effort cleanup if a multi-room insert fails partway through."""
    try:
        supabase.table("bookings").delete().eq("booking_reference", booking_ref).execute()
    except Exception as exc:  # noqa: BLE001
        logger.error("Rollback failed for %s: %s", booking_ref, exc)


def _persist_booking(check_in, check_out, result, rooms_req, guest,
                     special_requests, booking_ref):
    """Write one bookings row per reserved room in Supabase. Server-computed prices only."""
    ci, co = check_in.isoformat(), check_out.isoformat()
    nights = result["nights"]
    note = (special_requests or "").strip()[:1000] or None

    assignments, assign_err = _assign_physical_rooms(check_in, check_out, result)
    if assign_err:
        return {"ok": False, "error": assign_err}

    try:
        guest_id = _upsert_guest(guest)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Guest upsert failed: %s", exc)
        return {"ok": False, "error": "Could not save guest information. Please try again."}

    inserted = 0
    try:
        for idx, assignment in enumerate(assignments):
            req = rooms_req[idx] if idx < len(rooms_req) else {}
            sub = assignment["line_total"]
            gst, atl = _compute_taxes(sub, nights)
            line_grand = round(sub + gst + atl, 2)
            supabase.table("bookings").insert({
                "guest_id": guest_id,
                "room_id": assignment["room_id"],
                "check_in": ci,
                "check_out": co,
                "adults": _to_non_negative_int((req or {}).get("adults")) or 1,
                "children": _to_non_negative_int((req or {}).get("children")) or 0,
                "pets": _to_non_negative_int((req or {}).get("pets")) or 0,
                "booking_status": "confirmed",
                "amount_paid": 0.0,
                "room_price": assignment["rate"],
                "total_nights": nights,
                "total_price": line_grand,
                "booking_reference": booking_ref,
                "booking_notes": note,
            }).execute()
            inserted += 1
        return {"ok": True, "guest_id": guest_id}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Booking insert failed after %s row(s): %s", inserted, exc)
        if inserted:
            _rollback_bookings_by_reference(booking_ref)
        return {"ok": False, "error": "Could not store your booking. Please try again."}


@app.route('/confirm-booking', methods=['POST'])
@app.route('/api/complete-booking', methods=['POST'])
def handle_booking():
    """Finalize a reservation in Supabase. Re-runs validation immediately before write."""
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

    try:
        booking_ref = _generate_booking_reference()
    except RuntimeError as exc:
        logger.error(str(exc))
        return jsonify({"success": False, "error": "Could not create booking reference."}), 500

    persisted = _persist_booking(
        check_in, check_out, result, rooms_req,
        guest, data.get('special_requests'), booking_ref)

    if not persisted.get("ok"):
        return jsonify({
            "success": False,
            "error": persisted.get("error", "Could not store your booking."),
        }), 500

    confirmation = fetch_confirmation_from_supabase(supabase, booking_ref)
    email_sent, email_error = send_confirmation_email(app, confirmation)

    return jsonify({
        "success": True,
        "booking_reference": booking_ref,
        "redirect_url": f"/reservation-confirmation/{booking_ref}",
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

    booking_ref = "BK-" + secrets.token_hex(3).upper()
    _persist_booking(start_date, end_date, result,
                     [{"name": room, "adults": adults, "children": kids, "pets": pets}],
                     {}, None, booking_ref)

    return f"""
    <h1>Booking Saved!</h1>
    <p>We have recorded your stay for the <strong>{room}</strong>.</p>
    <p>Total for {result['nights']} night(s): <strong>${result['grand_total']:.2f}</strong></p>
    <p>Booking reference: <strong>{booking_ref}</strong></p>
    <a href='/'>Back to Home</a>
    """

@app.route('/admin-dashboard')
def admin_dashboard():
    init_db()
    conn = sqlite3.connect('bookings.db')
    db = conn.cursor()
    
    # 1. Get all active bookings
    db.execute("SELECT * FROM reservations")
    all_bookings = db.fetchall()
    
    # Create a list of currently occupied room types
    occupied_room_types = [b[1] for b in all_bookings]
    
    # 2. Define your 10 physical rooms and their types
    # In a real motel, you'd have room numbers
    rooms = [
        {"no": "101", "type": "Classic Queen Smoking"},
        {"no": "102", "type": "Classic Queen Non-Smoking"},
        {"no": "103", "type": "Double Queen Smoking"},
        {"no": "104", "type": "Double Queen Non-Smoking"},
        {"no": "105", "type": "Classic Queen Smoking"},
        {"no": "201", "type": "Classic Queen Non-Smoking"},
        {"no": "202", "type": "Double Queen Smoking"},
        {"no": "203", "type": "Double Queen Non-Smoking"},
        {"no": "204", "type": "Maintenance"}, # Manually set one to maintenance
        {"no": "301", "type": "Classic Queen Non-Smoking"}
    ]

    # 3. Logic to assign status
    for room in rooms:
        if room["type"] == "Maintenance":
            room["status"] = "maintenance"
        elif room["type"] in occupied_room_types:
            room["status"] = "occupied"
            # Remove from list so the next room of same type shows as available
            occupied_room_types.remove(room["type"])
        else:
            room["status"] = "available"

    # 4. Calculate Stats
    total_revenue = sum(b[7] for b in all_bookings)
    available_count = sum(1 for r in rooms if r["status"] == "available")
    occupancy_rate = (len(all_bookings) / 10) * 100

    conn.close()
    return render_template('admin.html', 
        bookings=all_bookings, 
        revenue=total_revenue,
        rooms=rooms,
        occupancy=occupancy_rate,
        available=available_count
    )

@app.route('/delete-booking/<int:booking_id>')
def delete_booking(booking_id):
    conn = sqlite3.connect('bookings.db')
    db = conn.cursor()
    # Delete the specific row using its ID
    db.execute("DELETE FROM reservations WHERE id = ?", (booking_id,))
    conn.commit()
    conn.close()
    
    # Send them back to the dashboard to see it's gone
    return redirect('/admin-dashboard')

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
    return render_template('final_details.html')


@app.route('/reservation-confirmation/<booking_ref>')
def reservation_confirmation(booking_ref):
    """Refresh-safe confirmation page loaded from Supabase by booking_reference."""
    ok, err = _supabase_required()
    if not ok:
        return render_template(
            'reservation_confirmation.html',
            confirmation=None,
            error=err,
            email_notice=None,
        ), 503

    confirmation = fetch_confirmation_from_supabase(supabase, booking_ref)
    if not confirmation:
        return render_template(
            'reservation_confirmation.html',
            confirmation=None,
            error="We could not find that reservation. Please check your booking reference or contact the lodge.",
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
def reservation_calendar(booking_ref):
    """Downloadable calendar event for the confirmed stay."""
    ok, err = _supabase_required()
    if not ok:
        return err, 503

    confirmation = fetch_confirmation_from_supabase(supabase, booking_ref)
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


@app.route('/privacy-policy')
def privacy_policy():
    return render_template('private_policy.html')

@app.route('/terms-and-conditions')
def terms_and_conditions():
    return render_template('terms_and_conditions.html')

if __name__ == '__main__':
    app.run(debug=True, port=5001)