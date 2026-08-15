"""Shared confirmation summary for the reservation page and email.

All dynamic reservation data is loaded from Supabase by booking_reference.
Static lodge information lives in LODGE constants below.
"""

import logging
import os
import re
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

from flask import render_template

logger = logging.getLogger(__name__)

LODG = {
    "name": "Grande Mountain Lodge",
    "phone": "780-827-2007",
    "phone_tel": "7808272007",
    "email": "reception@grandemountainlodge.com",
    "address": "9903 100 St, Grande Cache, AB T0E 0A6",
    "maps_url": "https://maps.google.com/?q=9903+100+St,+Grande+Cache,+AB+T0E+0A6",
    "parking": "Parking is free.",
    "check_in_hours": "3:00 PM - 9:00 PM",
    "check_out_hours": "11:00 AM",
    "min_check_in_age": 18,
    "payment_methods": (
        "Visa, Mastercard, American Express, Interac Debit, Cash, and E-Transfers"
    ),
    "cheques_note": "Cheques are not accepted.",
}

EMAIL_SUBJECT = "Reservation Confirmation — Grande Mountain Lodge"


def format_guest_date(value):
    """Format ISO date or date object for guest-facing display."""
    if not value:
        return None
    if isinstance(value, str):
        try:
            value = datetime.strptime(value[:10], "%Y-%m-%d").date()
        except ValueError:
            return value
    return value.strftime("%A, %d %b %Y")


def _guest_display_name(guest):
    if not guest:
        return None
    parts = [
        (guest.get("first_name") or "").strip(),
        (guest.get("last_name") or "").strip(),
    ]
    name = " ".join(p for p in parts if p)
    return name or None


def _guest_count_label(adults, children, pets):
    parts = []
    adults = adults or 0
    children = children or 0
    pets = pets or 0
    if adults:
        parts.append(f"{adults} {'Adult' if adults == 1 else 'Adults'}")
    if children:
        parts.append(f"{children} {'Child' if children == 1 else 'Children'}")
    if pets:
        parts.append(f"{pets} {'Pet' if pets == 1 else 'Pets'}")
    return ", ".join(parts) if parts else None


def _room_type_name(booking_row):
    room = booking_row.get("rooms") or {}
    room_type = room.get("room_types") or {}
    return room_type.get("name") or room.get("code")


def build_confirmation_context(booking_rows, guest):
    """Build a single confirmation dict from Supabase booking + guest rows."""
    if not booking_rows:
        return None

    primary = booking_rows[0]
    nights = primary.get("total_nights")
    if not nights and primary.get("check_in") and primary.get("check_out"):
        try:
            ci = datetime.strptime(primary["check_in"][:10], "%Y-%m-%d").date()
            co = datetime.strptime(primary["check_out"][:10], "%Y-%m-%d").date()
            nights = (co - ci).days
        except ValueError:
            nights = None

    rooms = []
    subtotal = 0.0
    for row in booking_rows:
        room = row.get("rooms") or {}
        rate = None
        room_type = room.get("room_types") or {}
        if room_type.get("nightly_rate") is not None:
            rate = float(room_type["nightly_rate"])
        line_subtotal = round((rate or 0) * (nights or 0), 2) if rate and nights else None
        if line_subtotal is not None:
            subtotal += line_subtotal
        rooms.append({
            "room_type_name": _room_type_name(row),
            "room_number": room.get("room_number"),
            "adults": row.get("adults"),
            "children": row.get("children"),
            "pets": row.get("pets"),
            "guest_count": _guest_count_label(
                row.get("adults"), row.get("children"), row.get("pets")
            ),
            "line_total": row.get("total_price"),
        })

    subtotal = round(subtotal, 2)
    grand_total = round(sum(float(r.get("total_price") or 0) for r in booking_rows), 2)
    gst = round(subtotal * 0.05, 2) if subtotal else None
    atl = round(subtotal * 0.06, 2) if subtotal and (nights or 0) < 28 else 0.0

    return {
        "lodge": LODG,
        "booking_reference": primary.get("booking_reference"),
        "check_in": format_guest_date(primary.get("check_in")),
        "check_out": format_guest_date(primary.get("check_out")),
        "check_in_iso": (primary.get("check_in") or "")[:10],
        "check_out_iso": (primary.get("check_out") or "")[:10],
        "nights": nights,
        "guest_name": _guest_display_name(guest),
        "guest_email": (guest or {}).get("email"),
        "guest_phone": (guest or {}).get("phone"),
        "guest_address": (guest or {}).get("address"),
        "guest_city": (guest or {}).get("city"),
        "guest_country": (guest or {}).get("country"),
        "rooms": rooms,
        "subtotal": subtotal if subtotal else None,
        "gst": gst,
        "atl": atl if atl else None,
        "grand_total": grand_total if grand_total else None,
        "special_requests": primary.get("booking_notes"),
        "booking_status": primary.get("booking_status"),
    }


def fetch_confirmation_from_supabase(supabase, booking_reference):
    """Load all booking rows + guest for a booking_reference from Supabase."""
    if not supabase or not booking_reference:
        return None

    ref = booking_reference.strip().upper()
    select_full = (
        "booking_id, booking_reference, guest_id, room_id, check_in, check_out, "
        "adults, children, pets, booking_status, amount_paid, total_nights, "
        "total_price, booking_notes, "
        "guests(guest_id, first_name, last_name, email, phone, address, city, country), "
        "rooms(room_id, room_number, code, status, room_type_id, "
        "room_types!rooms_room_type_id_fkey(room_type_id, name, code, nightly_rate))"
    )
    select_basic = (
        "booking_id, booking_reference, guest_id, room_id, check_in, check_out, "
        "adults, children, pets, booking_status, amount_paid, total_nights, "
        "total_price, booking_notes, "
        "guests(guest_id, first_name, last_name, email, phone, address, city, country), "
        "rooms(room_id, room_number, code, status, room_type_id)"
    )

    rows = None
    for select_clause in (select_full, select_basic):
        try:
            res = (
                supabase.table("bookings")
                .select(select_clause)
                .eq("booking_reference", ref)
                .execute()
            )
            rows = res.data or []
            if rows:
                break
        except Exception as exc:  # noqa: BLE001
            logger.warning("Confirmation query failed (%s): %s", select_clause[:40], exc)
            rows = None

    if not rows:
        return None

    # Enrich room type names when the nested join was unavailable.
    type_cache = {}
    for row in rows:
        room = row.get("rooms") or {}
        if room.get("room_types"):
            continue
        rt_id = room.get("room_type_id")
        if not rt_id:
            continue
        if rt_id not in type_cache:
            try:
                rt = (
                    supabase.table("room_types")
                    .select("room_type_id, name, code, nightly_rate")
                    .eq("room_type_id", rt_id)
                    .limit(1)
                    .execute()
                    .data
                )
                type_cache[rt_id] = rt[0] if rt else {}
            except Exception:  # noqa: BLE001
                type_cache[rt_id] = {}
        room["room_types"] = type_cache[rt_id]

    guest = rows[0].get("guests") or {}
    return build_confirmation_context(rows, guest)


def build_calendar_ics(confirmation):
    """Build an iCalendar (.ics) document for the guest stay."""
    if not confirmation:
        return None

    ci = confirmation.get("check_in_iso")
    co = confirmation.get("check_out_iso")
    if not ci or not co:
        return None

    ref = confirmation.get("booking_reference") or "Reservation"
    guest = confirmation.get("guest_name") or "Guest"
    lodge = confirmation.get("lodge") or LODG
    room_names = ", ".join(
        r.get("room_type_name") or "Room" for r in (confirmation.get("rooms") or [])
    )
    summary = f"Stay at {lodge['name']}"
    description = (
        f"Confirmation: {ref}\\n"
        f"Guest: {guest}\\n"
        f"Room(s): {room_names}\\n"
        f"Check-in from {lodge['check_in_hours']}\\n"
        f"Check-out until {lodge['check_out_hours']}\\n"
        f"Phone: {lodge['phone']}"
    )

    # Lodge local time (Grande Cache, AB)
    dtstart = f"{ci.replace('-', '')}T150000"
    dtend = f"{co.replace('-', '')}T110000"
    uid = f"{ref}@{lodge['email']}"

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Grande Mountain Lodge//Booking Confirmation//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}",
        f"DTSTART;TZID=America/Edmonton:{dtstart}",
        f"DTEND;TZID=America/Edmonton:{dtend}",
        f"SUMMARY:{summary}",
        f"DESCRIPTION:{description}",
        f"LOCATION:{lodge['address']}",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"


def send_confirmation_email(app, confirmation):
    """Send HTML + plain-text confirmation email. Returns (sent, error_message)."""
    if not confirmation:
        return False, "No confirmation data"

    to_addr = (confirmation.get("guest_email") or "").strip()
    if not to_addr:
        return False, "Guest email missing"

    enabled = os.getenv("SMTP_ENABLED", "true").lower() not in ("0", "false", "no")
    if not enabled:
        logger.warning("SMTP disabled; skipping confirmation email to %s", to_addr)
        return False, "SMTP disabled"

    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    from_addr = os.getenv("SMTP_FROM", LODG["email"]).strip()

    if not user or not password:
        logger.warning("SMTP credentials not configured; skipping email to %s", to_addr)
        return False, "SMTP not configured"

    html_body = render_template("confirmation_email.html", confirmation=confirmation)
    text_body = render_template("confirmation_email.txt", confirmation=confirmation)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = EMAIL_SUBJECT
    msg["From"] = formataddr((LODG["name"], from_addr))
    msg["To"] = to_addr
    msg["Reply-To"] = LODG["email"]
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.starttls()
            server.login(user, password)
            server.sendmail(from_addr, [to_addr], msg.as_string())
        logger.info("Confirmation email sent to %s for %s", to_addr, confirmation.get("booking_reference"))
        return True, None
    except smtplib.SMTPAuthenticationError:
        logger.error(
            "Gmail rejected SMTP login for %s. Use a Google App Password "
            "(Google Account → Security → 2-Step Verification → App passwords), "
            "not your regular Gmail password.",
            user,
        )
        return False, "Gmail authentication failed — use an App Password in SMTP_PASSWORD"
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to send confirmation email to %s: %s", to_addr, exc)
        return False, str(exc)


def validate_guest_payload(guest):
    """Server-side validation for guest fields collected on booker_contact."""
    guest = guest or {}
    errors = []

    first = (guest.get("first_name") or "").strip()
    last = (guest.get("last_name") or "").strip()
    email = (guest.get("email") or "").strip()
    phone = (guest.get("phone") or "").strip()
    address = (guest.get("address") or "").strip()
    city = (guest.get("city") or "").strip()
    country = (guest.get("country") or "").strip()

    if not first:
        errors.append("First name is required.")
    if not last:
        errors.append("Last name is required.")
    if not email or not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email):
        errors.append("A valid email address is required.")
    if not phone:
        errors.append("Phone number is required.")
    if not address:
        errors.append("Address is required.")
    if not city:
        errors.append("City is required.")
    if not country:
        errors.append("Country is required.")

    if errors:
        return False, " ".join(errors)

    return True, {
        "first_name": first[:100],
        "last_name": last[:100],
        "email": email,
        "phone": phone,
        "address": address[:200],
        "city": city[:100],
        "country": country,
    }
