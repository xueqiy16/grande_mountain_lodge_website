"""Narrowly-scoped tests for the pre-service-role hardening.

These cover the security-relevant server logic that does not need a live
Supabase: stay-window bounds, confirmation-token verification, idempotency-key
normalization, removal of legacy admin routes, and route rate limiting.
"""

from datetime import date, timedelta

import pytest

import main
from confirmation import _confirmation_token_ok


# --------------------------------------------------------------------------
# Step 6 — availability bounds
# --------------------------------------------------------------------------
def test_stay_window_rejects_past_checkin():
    today = date(2026, 1, 10)
    ok, err = main._validate_stay_window(date(2026, 1, 9), date(2026, 1, 11), today)
    assert not ok and "past" in err.lower()


def test_stay_window_rejects_checkout_not_after_checkin():
    today = date(2026, 1, 10)
    ok, _ = main._validate_stay_window(date(2026, 1, 12), date(2026, 1, 12), today)
    assert not ok


def test_stay_window_rejects_missing_dates():
    today = date(2026, 1, 10)
    assert not main._validate_stay_window(None, date(2026, 1, 12), today)[0]
    assert not main._validate_stay_window(date(2026, 1, 12), None, today)[0]


def test_stay_window_rejects_too_long_stay():
    today = date(2026, 1, 10)
    ci = date(2026, 1, 12)
    co = ci + timedelta(days=main.MAX_STAY_NIGHTS + 1)
    ok, err = main._validate_stay_window(ci, co, today)
    assert not ok and "nights" in err.lower()


def test_stay_window_rejects_too_far_future():
    today = date(2026, 1, 10)
    ci = today + timedelta(days=main.MAX_FUTURE_DAYS + 1)
    co = ci + timedelta(days=2)
    ok, err = main._validate_stay_window(ci, co, today)
    assert not ok and "future" in err.lower()


def test_stay_window_accepts_reasonable_stay():
    today = date(2026, 1, 10)
    ok, err = main._validate_stay_window(date(2026, 1, 12), date(2026, 1, 15), today)
    assert ok and err is None


def test_stay_window_allows_long_monthly_stay():
    # 28+ night stays (ATL-exempt) must remain bookable.
    today = date(2026, 1, 10)
    ci = date(2026, 1, 12)
    co = ci + timedelta(days=30)
    assert main._validate_stay_window(ci, co, today)[0]


# --------------------------------------------------------------------------
# Step 2 — confirmation token verification
# --------------------------------------------------------------------------
def test_verify_token_matches():
    assert main._verify_token("abc123", "abc123")
    assert _confirmation_token_ok("abc123", "abc123")


def test_verify_token_rejects_mismatch_and_absent():
    assert not main._verify_token("abc123", "nope")
    assert not main._verify_token("abc123", None)
    assert not main._verify_token(None, "abc123")
    assert not main._verify_token("", "")
    assert not _confirmation_token_ok(None, "x")
    assert not _confirmation_token_ok("x", None)


# --------------------------------------------------------------------------
# Step 4 — idempotency key normalization
# --------------------------------------------------------------------------
def test_idempotency_key_passthrough_valid():
    key = "idem-" + "a" * 32
    assert main._normalize_idempotency_key(key) == key


def test_idempotency_key_minted_when_invalid():
    for bad in (None, "", "short", 12345, "has spaces!!", "x" * 200):
        out = main._normalize_idempotency_key(bad)
        assert isinstance(out, str) and 16 <= len(out) <= 128
        assert out != bad


# --------------------------------------------------------------------------
# Step 7 — legacy admin routes are gone
# --------------------------------------------------------------------------
def test_legacy_admin_routes_removed():
    rules = {r.rule for r in main.app.url_map.iter_rules()}
    endpoints = {r.endpoint for r in main.app.url_map.iter_rules()}
    assert "/admin-dashboard" not in rules
    assert "admin_dashboard" not in endpoints
    assert "delete_booking" not in endpoints
    assert not any(rule.startswith("/delete-booking") for rule in rules)


def test_admin_routes_return_404():
    client = main.app.test_client()
    assert client.get("/admin-dashboard").status_code == 404
    assert client.get("/delete-booking/1").status_code == 404


# --------------------------------------------------------------------------
# Step 6 + Step 3 — availability endpoint validation & rate limiting
# --------------------------------------------------------------------------
@pytest.fixture
def client():
    main.app.config.update(TESTING=True)
    return main.app.test_client()


def test_availability_rejects_invalid_dates(client):
    main.limiter.enabled = False
    try:
        resp = client.post("/api/availability", json={"checkin": "2020-01-01", "checkout": "2020-01-02"})
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["valid"] is False
    finally:
        main.limiter.enabled = True


def test_availability_accepts_valid_dates(client):
    main.limiter.enabled = False
    try:
        ci = (date.today() + timedelta(days=3)).isoformat()
        co = (date.today() + timedelta(days=5)).isoformat()
        resp = client.post("/api/availability", json={"checkin": ci, "checkout": co})
        assert resp.status_code == 200
        assert resp.get_json()["valid"] is True
    finally:
        main.limiter.enabled = True


def test_availability_rate_limited(client):
    main.limiter.enabled = True
    main.limiter.reset()
    ci = (date.today() + timedelta(days=3)).isoformat()
    co = (date.today() + timedelta(days=5)).isoformat()
    saw_429 = False
    for _ in range(40):
        resp = client.post("/api/availability", json={"checkin": ci, "checkout": co})
        if resp.status_code == 429:
            saw_429 = True
            break
    main.limiter.reset()
    assert saw_429, "expected a 429 after exceeding the availability rate limit"


def test_booking_paused_page_renders(client):
    resp = client.get("/booking-paused")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Online Bookings Under Renovation" in body
    assert "Booking.com" in body
    assert "Expedia" in body


def test_booking_routes_redirect_while_paused(client):
    for path in ("/booking", "/book", "/bookings", "/booker_contact", "/final_details"):
        resp = client.get(path)
        assert resp.status_code in (301, 302), path
        assert "/booking-paused" in (resp.headers.get("Location") or "")


def test_confirm_booking_disabled_while_paused(client):
    main.limiter.enabled = False
    try:
        for path in ("/confirm-booking", "/api/complete-booking"):
            resp = client.post(path, json={})
            assert resp.status_code == 403, path
            body = resp.get_json()
            assert "temporarily disabled" in body["error"]
    finally:
        main.limiter.enabled = True

