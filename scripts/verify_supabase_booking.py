#!/usr/bin/env python3
"""Verify Supabase booking persistence end-to-end.

Requires SUPABASE_URL and a server-side Supabase credential
(SUPABASE_SECRET_KEY or SUPABASE_SERVICE_ROLE_KEY) in the environment
or a .env file.

Usage:
    python scripts/verify_supabase_booking.py
"""

from __future__ import annotations

import json
import hashlib
import os
import secrets
import sys
import uuid
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv

load_dotenv(os.path.join(ROOT, ".env"))

from main import (  # noqa: E402
    _assign_physical_rooms,
    _availability_by_name,
    _generate_booking_reference,
    _persist_booking,
    _validate_itinerary,
    supabase,
)
from confirmation import fetch_confirmation_from_supabase, validate_guest_payload  # noqa: E402

TEST_ROOM = "Studio Queen Non-Smoking"
TEST_NOTE = "Automated test booking"


def _cancel_prior_test_bookings():
    """Release inventory from earlier verify runs left in confirmed status."""
    if not supabase:
        return 0
    cancelled = 0
    try:
        rows = (
            supabase.table("bookings")
            .select("booking_reference, booking_status, booking_notes")
            .eq("booking_status", "confirmed")
            .execute()
            .data
            or []
        )
        for row in rows:
            note = (row.get("booking_notes") or "").strip()
            if note != TEST_NOTE:
                continue
            ref = row.get("booking_reference")
            if not ref:
                continue
            supabase.table("bookings").update({"booking_status": "cancelled"}).eq(
                "booking_reference", ref
            ).execute()
            cancelled += 1
            print("Cancelled prior test booking:", ref)
    except Exception as exc:  # noqa: BLE001
        print("Warning: could not clean up prior test bookings:", exc)
    return cancelled


def _find_open_dates(rooms_req, nights=2, start_offset=30, max_offset=365):
    """Return the first check-in/check-out window with availability for the cart."""
    for offset in range(start_offset, max_offset):
        check_in = date.today() + timedelta(days=offset)
        check_out = check_in + timedelta(days=nights)
        result, status = _validate_itinerary(check_in, check_out, rooms_req)
        if result.get("valid"):
            remaining = _availability_by_name(check_in, check_out).get(TEST_ROOM, 0)
            return check_in, check_out, remaining
    return None, None, 0


def main():
    if not supabase:
        print(
            "FAIL: Supabase client not configured. "
            "Set SUPABASE_URL and SUPABASE_SECRET_KEY "
            "(or SUPABASE_SERVICE_ROLE_KEY) in .env"
        )
        return 1

    # Preflight: room_types must be readable (requires service-role key)
    try:
        sample = supabase.table("room_types").select("room_type_id,code,name").limit(1).execute().data
    except Exception as exc:  # noqa: BLE001
        print("FAIL: cannot query room_types:", exc)
        return 1
    if not sample:
        print(
            "FAIL: room_types returned 0 rows.\n"
            "  • SUPABASE_KEY must be the service-role (secret) key, NOT sb_publishable_...\n"
            "  • In Supabase: Project Settings → API → service_role → Reveal secret\n"
            "  • Confirm room_types and rooms tables contain data in the Table Editor"
        )
        return 1
    print("Preflight OK — room_types accessible:", sample[0].get("code"), sample[0].get("name"))

    _cancel_prior_test_bookings()

    rooms_req = [{"name": TEST_ROOM, "adults": 2, "children": 0, "pets": 0}]
    check_in, check_out, remaining = _find_open_dates(rooms_req)
    if not check_in:
        print(
            "FAIL: no availability found for",
            TEST_ROOM,
            "in the next 365 days. Check rooms inventory / out-of-service status in Supabase.",
        )
        return 1
    print(f"Using dates {check_in} → {check_out} ({remaining} unit(s) available for {TEST_ROOM})")

    guest = {
        "first_name": "Test",
        "last_name": "Guest",
        "email": f"test.guest.{os.getpid()}@example.com",
        "phone": "780-555-0100",
        "address": "123 Test Street",
        "city": "Grande Cache",
        "country": "Canada",
    }

    ok, validated_guest = validate_guest_payload(guest)
    if not ok:
        print("FAIL: guest validation:", validated_guest)
        return 1

    result, status = _validate_itinerary(check_in, check_out, rooms_req)
    if not result.get("valid"):
        print("FAIL: itinerary validation:", status, result)
        return 1

    assignments, err = _assign_physical_rooms(check_in, check_out, result)
    if err:
        print("FAIL: room assignment:", err)
        return 1

    print("Room assignment preview:", json.dumps(assignments, indent=2, default=str))

    booking_ref = _generate_booking_reference()
    confirmation_token = secrets.token_urlsafe(32)
    cancellation_token_hash = hashlib.sha256(
        secrets.token_urlsafe(32).encode("utf-8")
    ).hexdigest()
    idempotency_key = "verify-" + uuid.uuid4().hex
    session_token_hash = None
    from payment_session import (
        generate_payment_session_token,
        hash_payment_session_token,
        uses_pending_payment_rpc,
    )
    if uses_pending_payment_rpc():
        session_token_hash = hash_payment_session_token(
            generate_payment_session_token()
        )
    persisted = _persist_booking(
        check_in, check_out, result, rooms_req, validated_guest, TEST_NOTE,
        booking_ref, confirmation_token, idempotency_key, cancellation_token_hash,
        session_token_hash=session_token_hash,
    )
    if not persisted.get("ok"):
        print("FAIL: persist:", persisted)
        return 1

    booking_ref = persisted.get("booking_reference", booking_ref)
    confirmation_token = persisted.get("confirmation_token", confirmation_token)
    confirmation = fetch_confirmation_from_supabase(supabase, booking_ref, token=confirmation_token)
    if not confirmation:
        print("FAIL: could not load confirmation for", booking_ref)
        try:
            supabase.table("bookings").update({"booking_status": "cancelled"}).eq(
                "booking_reference", booking_ref
            ).execute()
        except Exception:  # noqa: BLE001
            pass
        return 1

    print("SUCCESS: booking persisted")
    print(json.dumps(confirmation, indent=2, default=str))

    room_id = assignments[0]["room_id"]
    room_row = supabase.table("rooms").select("room_id, room_number, status, room_type_id, code").eq(
        "room_id", room_id
    ).single().execute().data
    print("Assigned room after booking:", room_row)
    if room_row.get("status") == "occupied":
        print(
            "NOTE: assigned room status is 'occupied' in DB "
            "(pre-existing operational state; booking insert does not change it)"
        )

    try:
        supabase.table("bookings").update({"booking_status": "cancelled"}).eq(
            "booking_reference", booking_ref
        ).execute()
        print("Marked test booking cancelled:", booking_ref)
    except Exception as exc:  # noqa: BLE001
        print("Cleanup: could not cancel test booking", booking_ref, "-", exc)
        print("You may cancel it manually in Supabase if needed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
