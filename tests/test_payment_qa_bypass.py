"""Temporary QA booking-pause bypass.

These tests never call Moneris or Supabase. They prove the pause still
holds for ordinary visitors, and that a short-lived HttpOnly cookie is
the only way past it — without skipping payment or pending_v7.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

import main
import payment_qa_bypass
from payment_qa_bypass import (
    COOKIE_NAME,
    MIN_QA_SECRET_LENGTH,
    QA_AUTH_PATH,
    QA_SECRET_ENV,
    TTL_SECONDS,
    configured_qa_secret,
)

WEBSITE_ROOT = Path(__file__).resolve().parents[1]
QA_SECRET = "q" * MIN_QA_SECRET_LENGTH
OTHER_SECRET = "z" * MIN_QA_SECRET_LENGTH
SHORT_SECRET = "s" * (MIN_QA_SECRET_LENGTH - 1)
BOOKING_PAGES = (
    "/booking",
    "/book",
    "/bookings",
    "/booker_contact",
    "/final_details",
    "/complete-payment",
)
BLOCKED_POSTS = (
    "/confirm-booking",
    "/api/complete-booking",
    "/api/complete-payment",
)
GUEST = {
    "first_name": "Ada",
    "last_name": "Lovelace",
    "email": "ada@example.com",
    "phone": "7805550100",
    "address": "1 Main St",
    "city": "Grande Cache",
    "country": "Canada",
}
ITINERARY = {
    "valid": True,
    "nights": 2,
    "subtotal": 200.0,
    "gst": 10.0,
    "atl": 8.0,
    "grand_total": 218.0,
}


@pytest.fixture
def client():
    main.app.config.update(TESTING=True)
    main.limiter.enabled = False
    try:
        yield main.app.test_client()
    finally:
        main.limiter.enabled = True


@pytest.fixture
def qa_secret(monkeypatch):
    monkeypatch.setenv(QA_SECRET_ENV, QA_SECRET)
    assert configured_qa_secret() == QA_SECRET


def _authorize(client):
    return client.post(QA_AUTH_PATH, json={"secret": QA_SECRET})


def _booking_json():
    ci = (date.today() + timedelta(days=10)).isoformat()
    co = (date.today() + timedelta(days=12)).isoformat()
    return {
        "checkin": ci,
        "checkout": co,
        "rooms": [
            {
                "name": "Studio Queen Non-Smoking",
                "adults": 2,
                "children": 0,
                "pets": 0,
            }
        ],
        "guest": GUEST,
        "idempotency_key": "idem-" + "a" * 32,
        "special_requests": "",
    }


def _sandbox_ht_env(monkeypatch):
    monkeypatch.setenv("MONERIS_ENV", "sandbox")
    monkeypatch.setenv(
        "MONERIS_HOSTED_TOKENIZATION_URL",
        "https://esqa.moneris.com/HPPtoken/index.php",
    )
    monkeypatch.setenv("MONERIS_HOSTED_TOKENIZATION_PROFILE_ID", "ht-profile-test-id")
    monkeypatch.setenv("MONERIS_CLIENT_SECRET", "unique-moneris-client-secret-xyz")
    monkeypatch.setenv("MONERIS_CLIENT_ID", "unique-moneris-client-id-xyz")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "unique-supabase-service-role-xyz")
    monkeypatch.setenv("MONERIS_API_BASE_URL", "https://api.sb.moneris.io")


def _patch_booking_flow(monkeypatch, persist_return, *, contract="pending_v7"):
    monkeypatch.setenv("CREATE_PUBLIC_BOOKING_CONTRACT", contract)
    monkeypatch.setattr(main, "_supabase_required", lambda: (True, None))
    monkeypatch.setattr(
        main, "_validate_itinerary", lambda *a, **k: (ITINERARY, 200)
    )
    monkeypatch.setattr(main, "_generate_booking_reference", lambda: "BK-ABC123")
    monkeypatch.setattr(main, "_persist_booking", persist_return)
    monkeypatch.setattr(
        main,
        "send_confirmation_email",
        lambda app, confirmation: (True, None),
    )
    monkeypatch.setattr(
        main,
        "fetch_confirmation_from_supabase",
        lambda *a, **k: {
            "booking_reference": "BK-ABC123",
            "guest_email": GUEST["email"],
        },
    )


def _assert_paused_redirect(resp, path):
    assert resp.status_code in (301, 302), path
    assert "/booking-paused" in (resp.headers.get("Location") or ""), path


def _assert_secret_absent(text):
    assert QA_SECRET not in text
    assert OTHER_SECRET not in text
    assert QA_SECRET_ENV not in text
    assert "PAYMENT_QA_BYPASS_SECRET" not in text


# ---------------------------------------------------------------------------
# Pause remains the default
# ---------------------------------------------------------------------------
def test_direct_bookings_remain_paused():
    assert main.DIRECT_BOOKINGS_PAUSED is True


def test_pause_constant_not_flipped_in_source():
    src = (WEBSITE_ROOT / "main.py").read_text(encoding="utf-8")
    assert "DIRECT_BOOKINGS_PAUSED = True" in src
    assert "DIRECT_BOOKINGS_PAUSED = False" not in src


# ---------------------------------------------------------------------------
# paused + no QA auth => still blocked
# ---------------------------------------------------------------------------
def test_paused_without_qa_auth_pages_still_redirect(client):
    assert main.DIRECT_BOOKINGS_PAUSED is True
    for path in BOOKING_PAGES:
        _assert_paused_redirect(client.get(path), path)


def test_paused_without_qa_auth_posts_still_403(client):
    for path in BLOCKED_POSTS:
        resp = client.post(path, json={})
        assert resp.status_code == 403, path
        body = resp.get_json()
        assert "temporarily disabled" in body["error"]


def test_qa_form_404_when_secret_unconfigured(client, monkeypatch):
    monkeypatch.delenv(QA_SECRET_ENV, raising=False)
    resp = client.get(QA_AUTH_PATH)
    assert resp.status_code == 404
    _assert_secret_absent(resp.get_data(as_text=True))


def test_short_secret_is_unconfigured(client, monkeypatch):
    monkeypatch.setenv(QA_SECRET_ENV, SHORT_SECRET)
    assert configured_qa_secret() is None
    assert client.get(QA_AUTH_PATH).status_code == 404
    resp = client.post(QA_AUTH_PATH, json={"secret": SHORT_SECRET})
    assert resp.status_code == 401
    _assert_paused_redirect(client.get("/booking"), "/booking")


# ---------------------------------------------------------------------------
# paused + invalid secret => still blocked
# ---------------------------------------------------------------------------
def test_invalid_secret_does_not_set_cookie_or_unpause(client, qa_secret):
    resp = client.post(QA_AUTH_PATH, json={"secret": OTHER_SECRET})
    assert resp.status_code == 401
    body = resp.get_json()
    assert body == {"ok": False, "error": "Unauthorized."}
    _assert_secret_absent(resp.get_data(as_text=True))
    assert COOKIE_NAME not in (resp.headers.get("Set-Cookie") or "")
    _assert_paused_redirect(client.get("/booking"), "/booking")
    confirm = client.post("/confirm-booking", json={})
    assert confirm.status_code == 403


def test_query_string_secret_is_ignored(client, qa_secret):
    resp = client.post(f"{QA_AUTH_PATH}?secret={QA_SECRET}", json={})
    assert resp.status_code == 401
    _assert_paused_redirect(client.get("/booking"), "/booking")


def test_valid_body_ignores_wrong_query_secret(client, qa_secret):
    resp = client.post(
        f"{QA_AUTH_PATH}?secret={OTHER_SECRET}",
        json={"secret": QA_SECRET},
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}
    booking = client.get("/booking")
    assert booking.status_code == 200


# ---------------------------------------------------------------------------
# paused + valid QA auth => booking pages reachable
# ---------------------------------------------------------------------------
def test_valid_qa_auth_sets_httponly_samesite_cookie(client, qa_secret):
    resp = _authorize(client)
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}
    cookie = resp.headers.get("Set-Cookie") or ""
    assert f"{COOKIE_NAME}=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie
    assert "Secure" not in cookie  # Flask TESTING
    _assert_secret_absent(cookie)
    _assert_secret_absent(resp.get_data(as_text=True))


def test_valid_qa_auth_makes_booking_pages_reachable(client, qa_secret, monkeypatch):
    monkeypatch.setenv("CREATE_PUBLIC_BOOKING_CONTRACT", "pending_v7")
    _sandbox_ht_env(monkeypatch)
    assert _authorize(client).status_code == 200
    for path in ("/booking", "/book", "/bookings", "/booker_contact", "/final_details"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        html = resp.get_data(as_text=True)
        _assert_secret_absent(html)
    pay = client.get("/complete-payment")
    assert pay.status_code == 200
    html = pay.get_data(as_text=True)
    assert "Complete your payment" in html
    _assert_secret_absent(html)


def test_form_post_redirects_to_booking_without_secret_in_url(client, qa_secret):
    resp = client.post(QA_AUTH_PATH, data={"secret": QA_SECRET})
    assert resp.status_code in (301, 302)
    location = resp.headers.get("Location") or ""
    assert location.endswith("/booking")
    assert "secret=" not in location.lower()
    _assert_secret_absent(location)
    assert client.get("/booking").status_code == 200


def test_unlisted_form_has_no_secret_or_js(client, qa_secret):
    resp = client.get(QA_AUTH_PATH)
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'method="post"' in html
    assert 'type="password"' in html
    assert "<script" not in html.lower()
    assert "localStorage" not in html
    assert "sessionStorage" not in html
    _assert_secret_absent(html)
    assert QA_SECRET not in html
    assert resp.headers.get("X-Robots-Tag") == "noindex, nofollow"


def test_get_with_secret_query_does_not_leak_or_authorize(client, qa_secret):
    resp = client.get(f"{QA_AUTH_PATH}?secret={QA_SECRET}")
    html = resp.get_data(as_text=True)
    _assert_secret_absent(html)
    assert QA_SECRET not in html
    _assert_paused_redirect(client.get("/booking"), "/booking")


# ---------------------------------------------------------------------------
# valid QA auth does NOT bypass payment
# ---------------------------------------------------------------------------
def test_qa_auth_does_not_skip_payment_completion(client, qa_secret, monkeypatch):
    monkeypatch.setenv("CREATE_PUBLIC_BOOKING_CONTRACT", "pending_v7")
    monkeypatch.setattr(main, "_supabase_required", lambda: (True, None))
    _sandbox_ht_env(monkeypatch)
    assert _authorize(client).status_code == 200
    html = client.get("/complete-payment").get_data(as_text=True)
    assert "var paymentEnabled = true" in html
    assert "htIframeSrc" in html
    empty = client.post("/api/complete-payment", json={})
    assert empty.status_code == 400
    body = empty.get_json()
    assert body["success"] is False
    assert "temporarily disabled" not in body["error"]
    _assert_secret_absent(empty.get_data(as_text=True))


def test_qa_auth_pending_v7_create_still_requires_payment(
    client, qa_secret, monkeypatch
):
    _patch_booking_flow(
        monkeypatch,
        lambda *a, **k: {
            "ok": True,
            "booking_reference": "BK-ABC123",
            "confirmation_token": "conf-token",
            "reused": False,
            "token_rotated": True,
        },
    )
    assert _authorize(client).status_code == 200
    resp = client.post("/confirm-booking", json=_booking_json())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    assert body["next_step"] == "payment"
    assert body["payment_url"] == "/complete-payment"
    assert "redirect_url" not in body
    assert "email_sent" not in body
    _assert_secret_absent(resp.get_data(as_text=True))


def test_qa_auth_does_not_grant_admin_or_cron(client, qa_secret, monkeypatch):
    monkeypatch.setenv("CREATE_PUBLIC_BOOKING_CONTRACT", "pending_v7")
    monkeypatch.setenv("PAYMENT_EXPIRY_CRON_SECRET", "c" * 32)
    monkeypatch.setenv("PAYMENT_RECONCILIATION_ADMIN_SECRET", "a" * 32)
    assert _authorize(client).status_code == 200
    expire = client.post("/api/internal/expire-payment-sessions")
    assert expire.status_code == 401
    held = client.get("/api/internal/payment-reconciliation/held")
    assert held.status_code == 401
    assert client.get("/admin-dashboard").status_code == 404


# ---------------------------------------------------------------------------
# QA secret absent from rendered HTML/JS/JSON
# ---------------------------------------------------------------------------
def test_qa_secret_absent_from_public_templates():
    for path in (WEBSITE_ROOT / "templates").glob("*.html"):
        text = path.read_text(encoding="utf-8")
        _assert_secret_absent(text)
        assert QA_AUTH_PATH not in text
        assert COOKIE_NAME not in text


def test_qa_secret_absent_from_static_js():
    static = WEBSITE_ROOT / "static"
    for path in static.rglob("*.js"):
        text = path.read_text(encoding="utf-8")
        _assert_secret_absent(text)
        assert QA_AUTH_PATH not in text


def test_logs_omit_qa_secret(client, qa_secret, caplog):
    caplog.set_level("DEBUG")
    client.post(QA_AUTH_PATH, json={"secret": QA_SECRET})
    client.post(QA_AUTH_PATH, json={"secret": OTHER_SECRET})
    client.get(f"{QA_AUTH_PATH}?secret={QA_SECRET}")
    _assert_secret_absent(caplog.text)


def test_secret_comparison_uses_compare_digest():
    src = (WEBSITE_ROOT / "payment_qa_bypass.py").read_text(encoding="utf-8")
    assert "secrets.compare_digest" in src
    assert "hmac.new" in src


def test_production_cookie_is_secure():
    main.app.config.update(TESTING=False)
    try:
        with main.app.test_request_context("/"):
            response = main.app.response_class("ok")
            payment_qa_bypass.apply_qa_cookie(response, QA_SECRET)
            cookie = response.headers.get("Set-Cookie") or ""
            assert "Secure" in cookie
            assert "HttpOnly" in cookie
            assert "SameSite=Lax" in cookie
            _assert_secret_absent(cookie)
    finally:
        main.app.config.update(TESTING=True)


# ---------------------------------------------------------------------------
# QA auth expires
# ---------------------------------------------------------------------------
def test_qa_auth_expires(client, qa_secret, monkeypatch):
    now = 1_700_000_000
    monkeypatch.setattr(payment_qa_bypass, "_now", lambda: now)
    assert _authorize(client).status_code == 200
    assert client.get("/booking").status_code == 200
    monkeypatch.setattr(payment_qa_bypass, "_now", lambda: now + TTL_SECONDS + 1)
    _assert_paused_redirect(client.get("/booking"), "/booking")
    confirm = client.post("/confirm-booking", json={})
    assert confirm.status_code == 403


def test_tampered_cookie_is_rejected(client, qa_secret):
    assert _authorize(client).status_code == 200
    client.set_cookie(COOKIE_NAME, "v1.9999999999." + ("ab" * 32))
    _assert_paused_redirect(client.get("/booking"), "/booking")


# ---------------------------------------------------------------------------
# Unpaused behavior unchanged
# ---------------------------------------------------------------------------
def test_unpaused_booking_pages_work_without_qa(client, monkeypatch):
    monkeypatch.delenv(QA_SECRET_ENV, raising=False)
    monkeypatch.setattr(main, "DIRECT_BOOKINGS_PAUSED", False)
    monkeypatch.setenv("CREATE_PUBLIC_BOOKING_CONTRACT", "pending_v7")
    _sandbox_ht_env(monkeypatch)
    for path in ("/booking", "/booker_contact", "/final_details", "/complete-payment"):
        resp = client.get(path)
        assert resp.status_code == 200, path
    confirm = client.post("/confirm-booking", json={})
    assert confirm.status_code != 403


def test_unpaused_pending_v7_create_unchanged(client, monkeypatch):
    monkeypatch.delenv(QA_SECRET_ENV, raising=False)
    monkeypatch.setattr(main, "DIRECT_BOOKINGS_PAUSED", False)
    _patch_booking_flow(
        monkeypatch,
        lambda *a, **k: {
            "ok": True,
            "booking_reference": "BK-ABC123",
            "confirmation_token": "conf-token",
            "reused": False,
            "token_rotated": True,
        },
    )
    resp = client.post("/confirm-booking", json=_booking_json())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["next_step"] == "payment"
    assert body["payment_url"] == "/complete-payment"


# ---------------------------------------------------------------------------
# pending_v7 remains required
# ---------------------------------------------------------------------------
def test_qa_auth_does_not_relax_pending_v7(client, qa_secret, monkeypatch):
    monkeypatch.setenv("CREATE_PUBLIC_BOOKING_CONTRACT", "live_v6")
    assert _authorize(client).status_code == 200
    pay = client.get("/complete-payment")
    assert pay.status_code == 404
    complete = client.post(
        "/api/complete-payment",
        json={
            "payment_session_token": "t" * 32,
            "dataKey": "d" * 26,
        },
    )
    assert complete.status_code == 404
    assert complete.get_json()["success"] is False
    _assert_secret_absent(complete.get_data(as_text=True))
