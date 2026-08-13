import os
import sqlite3
import secrets
from collections import Counter
from flask import Flask, render_template, request, redirect, jsonify
from datetime import datetime
from dotenv import load_dotenv
from supabase import create_client, Client

# ---------------------------------------------------------------------------
# SUPABASE CLIENT
# Loaded from .env. The client is optional at runtime: if credentials are
# missing or the service is unreachable, the server falls back to the local
# SQLite store so the site keeps working (defensive by design).
# ---------------------------------------------------------------------------
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:  # noqa: BLE001
        print(f"[Supabase] client init failed, using local fallback: {e}")
        supabase = None
else:
    print("[Supabase] SUPABASE_URL / SUPABASE_KEY not set, using local fallback.")

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
# public.rooms tracks the room's IMMEDIATE physical/operational condition in a
# single text `status` column: 'available', 'occupied', 'house-keeping',
# 'out-of-service'. ('reserved' no longer exists here.)
#
# A room's eligibility for a FUTURE date window is decided by calendar overlaps
# in public.bookings — NOT by its momentary status. A room that is 'occupied'
# today (or booked in December) is still part of the bookable inventory for an
# open window in August. Therefore the physical inventory base counts every room
# EXCEPT those that are physically unusable right now: mid-clean or under repair.
NON_BOOKABLE_ROOM_STATUSES = ("house-keeping", "out-of-service")

# --- Tax configuration (Alberta) ---
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


def _room_types_by_code():
    """{code: {room_type_id, name, rate}} from Supabase, falling back to the
    static ROOM_CATALOG when Supabase is unavailable."""
    global _ROOM_TYPES_CACHE
    if _ROOM_TYPES_CACHE is not None:
        return _ROOM_TYPES_CACHE
    data = {}
    if supabase:
        try:
            rows = supabase.table("room_types").select(
                "room_type_id,name,code,nightly_rate").execute().data
            for r in rows:
                data[r["code"]] = {
                    "room_type_id": r["room_type_id"],
                    "name": r["name"],
                    "rate": float(r["nightly_rate"]),
                }
        except Exception as e:  # noqa: BLE001
            print(f"[Supabase] room_types fetch failed: {e}")
    if not data:
        for name, info in ROOM_CATALOG.items():
            data[info["code"]] = {"room_type_id": None, "name": name, "rate": info["rate"]}
    _ROOM_TYPES_CACHE = data
    return data


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
            # Exclude only maintenance/housekeeping; never filter on 'reserved'
            # or the momentary 'occupied' flag.
            query = supabase.table("rooms").select("code,status")
            for blocked in NON_BOOKABLE_ROOM_STATUSES:
                query = query.neq("status", blocked)
            rows = query.execute().data
            for r in rows:
                code = r.get("code")
                if code:
                    counts[code] = counts.get(code, 0) + 1
        except Exception as e:  # noqa: BLE001
            print(f"[Supabase] rooms inventory fetch failed: {e}")
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
    """Count of overlapping, inventory-blocking bookings per room-type code for
    the requested [check_in, check_out) window. Supabase first, SQLite fallback.

    Half-open overlap rule: an existing booking conflicts with (req_in, req_out)
    IFF existing_check_in < req_out AND existing_check_out > req_in — so a
    same-day turnover (guest departing on the requested check-in date) does NOT
    block a new arrival. Cancelled and no-show bookings never block inventory."""
    ci, co = check_in.isoformat(), check_out.isoformat()
    counts = {}
    if supabase:
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
            return counts
        except Exception as e:  # noqa: BLE001
            print(f"[Supabase] overlap query failed, using SQLite fallback: {e}")
    return _overlap_counts_sqlite(check_in, check_out)


def _overlap_counts_sqlite(check_in, check_out):
    """Local fallback overlap counter keyed by room-type code.

    Date filtering is pushed to SQL (half-open interval on the ISO text columns)
    so the idx_reservations_dates index is used; the Python loop below then
    re-validates each row via _parse_date and drops any unparseable rows."""
    counts = {}
    ci, co = check_in.isoformat(), check_out.isoformat()
    try:
        conn = sqlite3.connect('bookings.db')
        db = conn.cursor()
        db.execute(
            "SELECT room_type, checkin, checkout FROM reservations "
            "WHERE checkin < ? AND checkout > ?",
            (co, ci))
        rows = db.fetchall()
        conn.close()
    except Exception:  # noqa: BLE001
        return counts
    for name, s_ci, s_co in rows:
        info = ROOM_CATALOG.get(name)
        if not info:
            continue
        d1 = _parse_date(s_ci)
        d2 = _parse_date(s_co)
        if not d1 or not d2:
            continue
        if d1 < check_out and d2 > check_in:
            code = info["code"]
            counts[code] = counts.get(code, 0) + 1
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

# This creates the database file and the table if they don't exist
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

init_db()

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
# BOOKING PERSISTENCE (Supabase, with SQLite fallback)
# ---------------------------------------------------------------------------
def _parse_expiry(s):
    """'MM / YY' (or similar) -> (month:int, year:int) e.g. (12, 2029)."""
    digits = ''.join(ch for ch in str(s or '') if ch.isdigit())
    if len(digits) < 4:
        return None, None
    try:
        return int(digits[:2]), 2000 + int(digits[2:4])
    except ValueError:
        return None, None


def _upsert_guest(guest):
    """Find a guest by email (update) or insert a new one. Returns guest_id."""
    email = (guest.get("email") or "").strip()
    payload = {
        "first_name": (guest.get("first_name") or "").strip()[:100] or None,
        "last_name": (guest.get("last_name") or "").strip()[:100] or None,
        "email": email or None,
        "phone": (guest.get("phone") or "").strip() or None,
        "address": (guest.get("address") or "").strip()[:200] or None,
        "city": (guest.get("city") or "").strip()[:100] or None,
        "country": (guest.get("country") or "").strip() or None,
    }
    if email:
        found = supabase.table("guests").select("guest_id").eq("email", email).limit(1).execute().data
        if found:
            gid = found[0]["guest_id"]
            supabase.table("guests").update(payload).eq("guest_id", gid).execute()
            return gid
    return supabase.table("guests").insert(payload).execute().data[0]["guest_id"]


def _available_room_ids(code, check_in, check_out):
    """Concrete room_ids of a given type that are (a) physically usable right now
    (status not in house-keeping / out-of-service) and (b) have ZERO overlapping
    inventory-blocking bookings for the requested [check_in, check_out) window.

    A currently 'occupied' room is eligible so long as it has no booking that
    overlaps the requested dates — this is the concrete room-assignment guard."""
    ci, co = check_in.isoformat(), check_out.isoformat()
    rooms_query = (supabase.table("rooms")
                   .select("room_id")
                   .eq("code", code))
    for blocked in NON_BOOKABLE_ROOM_STATUSES:
        rooms_query = rooms_query.neq("status", blocked)
    rooms_of_code = rooms_query.execute().data
    ids = [r["room_id"] for r in rooms_of_code]
    if not ids:
        return []
    busy = (supabase.table("bookings").select("room_id")
            .in_("room_id", ids)
            .lt("check_in", co).gt("check_out", ci)
            .neq("booking_status", "cancelled")
            .neq("booking_status", "no_show").execute().data)
    busy_ids = {b["room_id"] for b in busy}
    return [i for i in ids if i not in busy_ids]


def _persist_booking(check_in, check_out, result, rooms_req, guest, card,
                     special_requests, booking_ref, token):
    """Write one bookings row per reserved room. Server-computed prices only.
    Returns {"ok": bool, "error"?: str}."""
    ci, co = check_in.isoformat(), check_out.isoformat()
    nights = result["nights"]

    if supabase:
        try:
            guest_id = _upsert_guest(guest)
            exp_month, exp_year = _parse_expiry(card.get("expiry"))
            brand = (card.get("brand") or "").strip().lower() or None
            last4 = (card.get("last4") or "").strip()[-4:] or None
            cardholder = (card.get("cardholder_name")
                          or f"{guest.get('first_name', '')} {guest.get('last_name', '')}".strip()
                          or None)
            note = (special_requests or "").strip()[:1000] or None

            for idx, vr in enumerate(result["rooms"]):
                free = _available_room_ids(vr["code"], check_in, check_out)
                if not free:
                    return {"ok": False,
                            "error": "One or more requested rooms are no longer available for these dates."}
                req = rooms_req[idx] if idx < len(rooms_req) else {}
                sub = vr["line_total"]
                gst, atl = _compute_taxes(sub, nights)
                line_grand = round(sub + gst + atl, 2)
                supabase.table("bookings").insert({
                    "guest_id": guest_id,
                    "room_id": free[0],
                    "check_in": ci,
                    "check_out": co,
                    "adults": _to_non_negative_int((req or {}).get("adults")) or 1,
                    "children": _to_non_negative_int((req or {}).get("children")) or 0,
                    "pets": _to_non_negative_int((req or {}).get("pets")) or 0,
                    "booking_status": "confirmed",
                    "moneris_token": token,
                    "guarantee_method": brand,
                    "last4": last4,
                    "expiry_month": exp_month,
                    "expiry_year": exp_year,
                    "amount_paid": 0.0,          # no upfront payment
                    "total_nights": nights,
                    "total_price": line_grand,
                    "card_holder_name": cardholder,
                    "booking_reference": booking_ref,
                    "booking_notes": note,
                }).execute()
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            print(f"[Supabase] booking persist failed, using SQLite fallback: {e}")

    # SQLite fallback so a reservation is never silently lost
    try:
        conn = sqlite3.connect('bookings.db')
        db = conn.cursor()
        for idx, vr in enumerate(result["rooms"]):
            req = rooms_req[idx] if idx < len(rooms_req) else {}
            db.execute('''
                INSERT INTO reservations (room_type, checkin, checkout, adults, children, pets, total_price)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (vr["name"], ci, co,
                  _to_non_negative_int((req or {}).get("adults")) or 1,
                  _to_non_negative_int((req or {}).get("children")) or 0,
                  _to_non_negative_int((req or {}).get("pets")) or 0,
                  vr["line_total"]))
        conn.commit()
        conn.close()
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@app.route('/confirm-booking', methods=['POST'])
@app.route('/api/complete-booking', methods=['POST'])
def handle_booking():
    """Finalize a reservation. Re-runs itinerary verification (inventory +
    server re-pricing) immediately before storing the booking and moneris
    token to Supabase. Client-submitted totals are never trusted or stored."""
    data = request.get_json(silent=True)

    # Legacy single-room HTML form fallback (kept for backwards compatibility)
    if data is None:
        return _legacy_form_booking()

    check_in = _parse_date(data.get('checkin'))
    check_out = _parse_date(data.get('checkout'))
    rooms_req = data.get('rooms')

    # 1. Re-verify + re-price on the server (source of truth)
    result, status = _validate_itinerary(check_in, check_out, rooms_req)
    if not result.get("valid"):
        return jsonify({"success": False, "error": result.get("error")}), status

    # 2. Persist with a freshly generated booking reference + moneris token
    booking_ref = "BK-" + secrets.token_hex(3).upper()
    token = "RES-" + secrets.token_hex(4).upper()
    persisted = _persist_booking(
        check_in, check_out, result, rooms_req,
        data.get('guest') or {}, data.get('card') or {},
        data.get('special_requests'), booking_ref, token)

    if not persisted.get("ok"):
        return jsonify({"success": False,
                        "error": persisted.get("error", "Could not store your booking.")}), 500

    return jsonify({
        "success": True,
        "booking_reference": booking_ref,
        "nights": result["nights"],
        "subtotal": result["subtotal"],
        "gst": result["gst"],
        "atl": result["atl"],
        "grand_total": result["grand_total"],
    }), 200


def _legacy_form_booking():
    """Original single-room form handler (used only for non-JSON posts)."""
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
    token = "RES-" + secrets.token_hex(4).upper()
    _persist_booking(start_date, end_date, result,
                     [{"name": room, "adults": adults, "children": kids, "pets": pets}],
                     {}, {}, None, booking_ref, token)

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

@app.route('/privacy-policy')
def privacy_policy():
    return render_template('private_policy.html')

@app.route('/terms-and-conditions')
def terms_and_conditions():
    return render_template('terms_and_conditions.html')

if __name__ == '__main__':
    app.run(debug=True, port=5001)