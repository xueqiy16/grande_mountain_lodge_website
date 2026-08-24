"""TEMPORARY production card-validation test path.

Mocks Moneris. Does not call the network, execute SQL, or enter a card.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

import main
import payment_prod_validation_test as prod_test
from payment_api.config import (
    PRODUCTION_API_BASE_URL,
    PRODUCTION_HOSTED_TOKENIZATION_URL,
    REQUIRED_API_VERSION,
)
from payment_completion import PaymentCompletionError
from payment_prod_validation_test import (
    COOKIE_NAME,
    QA_SPECIAL_REQUESTS,
    START_PATH,
    mint_capability,
    parse_capability,
    select_qa_stay,
)
from payment_session import hash_payment_session_token


WEBSITE_ROOT = Path(__file__).resolve().parents[1]
HT_JS = WEBSITE_ROOT / "static" / "complete_payment_ht.js"
VALIDATION_PY = WEBSITE_ROOT / "payment_api" / "validation.py"
MODULE_PY = WEBSITE_ROOT / "payment_prod_validation_test.py"
COMPLETE_TEMPLATE = WEBSITE_ROOT / "templates" / "complete_payment.html"

TEST_SECRET = "prod-val-test-secret-do-not-reuse-32x"
RECON_SECRET = "recon-admin-secret-value-32chars-aa"
CRON_SECRET = "expiry-cron-secret-value-32chars-bb"
CANCEL_SECRET = "cancellation-token-secret-32ch-cc"
QA_EMAIL = "gml-qa-validation@example.test"
RAW_TOKEN = "B" * 43
OTHER_TOKEN = "C" * 43
DATA_KEY = "K" * 26
BOOKING_REF = "BK-QA0001"
CANONICAL = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
RESERVATION = "11111111-2222-3333-4444-555555555555"
PAYMENT_SESSION = "99999999-8888-7777-6666-555555555555"
PAYMENT_METHOD = "pm_leak_test_id"
ISSUER = "issuer_leak_test_id"

assert len(TEST_SECRET) >= 32
assert len({TEST_SECRET, RECON_SECRET, CRON_SECRET, CANCEL_SECRET}) == 4


def _forbid_network(monkeypatch):
    def fail(*_args, **_kwargs):
        raise AssertionError("network request is forbidden in prod-validation tests")

    monkeypatch.setattr(requests, "request", fail)
    monkeypatch.setattr(requests, "post", fail)
    monkeypatch.setattr(requests, "get", fail)
    monkeypatch.setattr(requests, "put", fail)
    monkeypatch.setattr(requests, "patch", fail)
    monkeypatch.setattr(requests, "delete", fail)


def _apply_env(monkeypatch, env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def _production_moneris_env(**overrides):
    env = {
        "MONERIS_ENV": "production",
        "MONERIS_CLIENT_ID": "unique-moneris-client-id-xyz",
        "MONERIS_CLIENT_SECRET": "unique-moneris-client-secret-xyz",
        "MONERIS_MERCHANT_ID": "unique-moneris-merchant-xyz",
        "MONERIS_API_VERSION": REQUIRED_API_VERSION,
        "MONERIS_API_BASE_URL": PRODUCTION_API_BASE_URL,
        "MONERIS_HOSTED_TOKENIZATION_PROFILE_ID": "ht-profile-test-id",
        "MONERIS_HOSTED_TOKENIZATION_URL": PRODUCTION_HOSTED_TOKENIZATION_URL,
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_SERVICE_ROLE_KEY": "unique-supabase-service-role-xyz",
        "CREATE_PUBLIC_BOOKING_CONTRACT": "pending_v7",
        "CANCELLATION_TOKEN_SECRET": CANCEL_SECRET,
        "PAYMENT_PROD_VALIDATION_TEST_SECRET": TEST_SECRET,
        "PAYMENT_PROD_VALIDATION_TEST_EMAIL": QA_EMAIL,
        "PAYMENT_RECONCILIATION_ADMIN_SECRET": RECON_SECRET,
        "PAYMENT_EXPIRY_CRON_SECRET": CRON_SECRET,
    }
    env.update(overrides)
    return env


def _valid_itinerary(_check_in, _check_out, rooms_req):
    return (
        {
            "valid": True,
            "ok": True,
            "nights": 1,
            "subtotal": 89.99,
            "gst": 4.50,
            "atl": 5.40,
            "grand_total": 99.89,
            "rooms": rooms_req,
        },
        200,
    )


def _unavailable_itinerary(_check_in, _check_out, _rooms_req):
    return ({"valid": False, "ok": False, "error": "unavailable"}, 409)


class PersistBox:
    def __init__(self, ok=True):
        self.calls = []
        self.ok = ok

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if not self.ok:
            return {"ok": False, "error": "Could not store your booking."}
        return {
            "ok": True,
            "booking_reference": BOOKING_REF,
            "confirmation_token": "confirmation-token-must-not-leak",
            "reused": False,
            "token_rotated": True,
            "canonical_booking_id": CANONICAL,
            "reservation_id": RESERVATION,
            "payment_session_id": PAYMENT_SESSION,
            "paymentMethodId": PAYMENT_METHOD,
            "issuerId": ISSUER,
            "dataKey": DATA_KEY,
        }


def _cookie_header(resp):
    return "\n".join(resp.headers.getlist("Set-Cookie"))


def _cookie_value(resp, name=COOKIE_NAME):
    for header in resp.headers.getlist("Set-Cookie"):
        if header.startswith(name + "="):
            return header.split(";", 1)[0].split("=", 1)[1]
    return None


def _set_capability(client, raw_token, monkeypatch):
    monkeypatch.setenv("MONERIS_ENV", "production")
    monkeypatch.setenv("PAYMENT_PROD_VALIDATION_TEST_SECRET", TEST_SECRET)
    value = mint_capability(hash_payment_session_token(raw_token))
    client.set_cookie(
        COOKIE_NAME,
        value,
        path="/",
        secure=True,
        httponly=True,
        samesite="Strict",
    )
    return value


def _handoff_token(html):
    match = re.search(
        r'sessionStorage\.setItem\("gml_payment_session_token", (".*?")\)',
        html,
    )
    assert match, html
    return json.loads(match.group(1))


def _assert_no_sensitive(text, extra=()):
    blob = text if isinstance(text, str) else json.dumps(text, default=str)
    for sentinel in (
        TEST_SECRET,
        RECON_SECRET,
        CRON_SECRET,
        CANCEL_SECRET,
        CANONICAL,
        RESERVATION,
        PAYMENT_SESSION,
        PAYMENT_METHOD,
        ISSUER,
        DATA_KEY,
        QA_EMAIL,
        BOOKING_REF,
    ) + tuple(extra):
        assert sentinel not in blob, f"leaked {sentinel!r}"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    _forbid_network(monkeypatch)


@pytest.fixture
def client():
    main.app.config.update(TESTING=True)
    main.limiter.enabled = False
    try:
        test_client = main.app.test_client()
        test_client.environ_base["wsgi.url_scheme"] = "https"
        yield test_client
    finally:
        main.limiter.enabled = True


@pytest.fixture
def start_stack(monkeypatch):
    persist = PersistBox()
    _apply_env(monkeypatch, _production_moneris_env())
    monkeypatch.setattr(main, "_supabase_required", lambda: (True, None))
    monkeypatch.setattr(main, "_validate_itinerary", _valid_itinerary)
    monkeypatch.setattr(main, "_generate_booking_reference", lambda: BOOKING_REF)
    monkeypatch.setattr(main, "_persist_booking", persist)
    return persist


# ---------------------------------------------------------------------------
# Pause + markers
# ---------------------------------------------------------------------------
def test_direct_bookings_remain_paused():
    assert main.DIRECT_BOOKINGS_PAUSED is True
    src = Path(main.__file__).read_text(encoding="utf-8")
    assert "DIRECT_BOOKINGS_PAUSED = True" in src
    blocked = Path(main.__file__).read_text(encoding="utf-8")
    assert "def _booking_funnel_blocked():" in blocked
    fn = inspect_funnel_blocked()
    assert "DIRECT_BOOKINGS_PAUSED" in fn
    assert "capability" not in fn


def inspect_funnel_blocked():
    src = Path(main.__file__).read_text(encoding="utf-8")
    start = src.index("def _booking_funnel_blocked():")
    return src[start:src.index("\n\n", start)]


def test_temporary_markers_present():
    module = MODULE_PY.read_text(encoding="utf-8")
    assert "TEMPORARY" in module
    assert "TEMPORARY" in (WEBSITE_ROOT / "templates" / "prod_validation_test_auth.html").read_text(
        encoding="utf-8"
    )
    assert "TEMPORARY" in (
        WEBSITE_ROOT / "templates" / "prod_validation_test_handoff.html"
    ).read_text(encoding="utf-8")
    main_src = Path(main.__file__).read_text(encoding="utf-8")
    assert "TEMPORARY" in main_src
    env = (WEBSITE_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "PAYMENT_PROD_VALIDATION_TEST_SECRET=" in env
    assert "PAYMENT_PROD_VALIDATION_TEST_EMAIL=" in env
    assert env.count("TEMPORARY — remove immediately after production card-validation test.") >= 2


def test_real_complete_payment_template_reused():
    html = COMPLETE_TEMPLATE.read_text(encoding="utf-8")
    assert "complete_payment_ht.js" in html
    assert "gml_payment_session_token" in html
    handoff = (WEBSITE_ROOT / "templates" / "prod_validation_test_handoff.html").read_text(
        encoding="utf-8"
    )
    assert "complete_payment_ht.js" not in handoff
    assert "<iframe" not in handoff.lower()


def test_complete_payment_ht_js_unchanged():
    result = subprocess.run(
        ["git", "diff", "--", "static/complete_payment_ht.js"],
        cwd=WEBSITE_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout == ""
    js = HT_JS.read_text(encoding="utf-8")
    assert "prod-card-validation-test" not in js
    assert COOKIE_NAME not in js


def test_validation_py_unchanged():
    result = subprocess.run(
        ["git", "diff", "--", "payment_api/validation.py"],
        cwd=WEBSITE_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout == ""
    src = VALIDATION_PY.read_text(encoding="utf-8")
    assert 'VALIDATIONS_PATH = "/validations"' in src
    assert "/payments" not in src


def test_no_charge_surface_in_temporary_path():
    module = MODULE_PY.read_text(encoding="utf-8")
    assert re.search(r'["\']/payments["\']', module) is None
    assert "automaticCapture" not in module
    assert "pre_auth" not in module
    assert "pre-authorization" not in module
    assert "validate_card" not in module
    assert "from payment_api.validation" not in module
    assert re.search(r'["\']/(purchase|capture|refund|payments)', module) is None
    main_src = Path(main.__file__).read_text(encoding="utf-8")
    start = main_src.index("def prod_card_validation_test():")
    end = main_src.index("def handle_expire_payment_sessions", start)
    temporary_route = main_src[start:end]
    assert re.search(r'["\']/payments["\']', temporary_route) is None
    assert "automaticCapture" not in temporary_route
    assert "pre_auth" not in temporary_route
    complete = main_src[
        main_src.index("def handle_complete_payment():") : main_src.index(
            "def prod_card_validation_test():"
        )
    ]
    assert "parse_browser_payment_request(payload)" in complete
    assert "complete_pending_payment(" in complete


def test_compare_digest_used():
    src = MODULE_PY.read_text(encoding="utf-8")
    assert "secrets.compare_digest" in src
    assert "hmac.compare_digest" in src


# ---------------------------------------------------------------------------
# GET start route
# ---------------------------------------------------------------------------
def test_get_start_creates_nothing_and_has_no_iframe(client, start_stack):
    persist = start_stack
    resp = client.get(START_PATH)
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert persist.calls == []
    assert 'method="POST"' in html
    assert 'name="secret"' in html
    assert "<iframe" not in html.lower()
    assert "HPPtoken" not in html
    assert "complete_payment_ht.js" not in html
    assert TEST_SECRET not in html
    assert COOKIE_NAME not in _cookie_header(resp)
    _assert_no_sensitive(html)


def test_get_start_does_not_echo_query_secret(client, start_stack):
    resp = client.get(f"{START_PATH}?secret={TEST_SECRET}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert TEST_SECRET not in html
    assert start_stack.calls == []


# ---------------------------------------------------------------------------
# Auth / fail-closed
# ---------------------------------------------------------------------------
def test_missing_test_secret_config_is_503(client, start_stack, monkeypatch):
    monkeypatch.delenv("PAYMENT_PROD_VALIDATION_TEST_SECRET", raising=False)
    resp = client.post(START_PATH, data={"secret": TEST_SECRET})
    assert resp.status_code == 503
    assert start_stack.calls == []


def test_short_test_secret_is_503(client, start_stack, monkeypatch):
    monkeypatch.setenv("PAYMENT_PROD_VALIDATION_TEST_SECRET", "short-secret")
    resp = client.post(START_PATH, data={"secret": "short-secret"})
    assert resp.status_code == 503
    assert start_stack.calls == []


def test_wrong_secret_is_401(client, start_stack):
    resp = client.post(START_PATH, data={"secret": "definitely-not-the-test-secret-value"})
    assert resp.status_code == 401
    assert start_stack.calls == []
    assert TEST_SECRET not in resp.get_data(as_text=True)


def test_query_string_secret_cannot_authorize(client, start_stack):
    resp = client.post(f"{START_PATH}?secret={TEST_SECRET}", data={})
    assert resp.status_code == 401
    assert start_stack.calls == []


def test_authorization_bearer_cannot_authorize(client, start_stack):
    resp = client.post(
        START_PATH,
        data={},
        headers={"Authorization": f"Bearer {TEST_SECRET}"},
    )
    assert resp.status_code == 401
    assert start_stack.calls == []


def test_json_body_secret_cannot_authorize(client, start_stack):
    resp = client.post(START_PATH, json={"secret": TEST_SECRET})
    assert resp.status_code == 401
    assert start_stack.calls == []


def test_reconciliation_secret_cannot_authorize(client, start_stack):
    resp = client.post(START_PATH, data={"secret": RECON_SECRET})
    assert resp.status_code == 401
    assert start_stack.calls == []


def test_expiry_cron_secret_cannot_authorize(client, start_stack):
    resp = client.post(START_PATH, data={"secret": CRON_SECRET})
    assert resp.status_code == 401
    assert start_stack.calls == []


def test_cancellation_secret_cannot_authorize(client, start_stack):
    resp = client.post(START_PATH, data={"secret": CANCEL_SECRET})
    assert resp.status_code == 401
    assert start_stack.calls == []


def test_reused_secret_config_fails_closed(client, start_stack, monkeypatch):
    monkeypatch.setenv("PAYMENT_PROD_VALIDATION_TEST_SECRET", RECON_SECRET)
    resp = client.post(START_PATH, data={"secret": RECON_SECRET})
    assert resp.status_code == 503
    assert start_stack.calls == []


def test_non_production_moneris_env_is_503(client, start_stack, monkeypatch):
    monkeypatch.setenv("MONERIS_ENV", "sandbox")
    resp = client.post(START_PATH, data={"secret": TEST_SECRET})
    assert resp.status_code == 503
    assert start_stack.calls == []


def test_missing_pending_v7_is_503(client, start_stack, monkeypatch):
    monkeypatch.setenv("CREATE_PUBLIC_BOOKING_CONTRACT", "live_v6")
    resp = client.post(START_PATH, data={"secret": TEST_SECRET})
    assert resp.status_code == 503
    assert start_stack.calls == []


def test_invalid_contract_is_503(client, start_stack, monkeypatch):
    monkeypatch.setenv("CREATE_PUBLIC_BOOKING_CONTRACT", "pending")
    resp = client.post(START_PATH, data={"secret": TEST_SECRET})
    assert resp.status_code == 503
    assert start_stack.calls == []


def test_no_available_room_fails_safely(client, start_stack, monkeypatch):
    monkeypatch.setattr(main, "_validate_itinerary", _unavailable_itinerary)
    resp = client.post(START_PATH, data={"secret": TEST_SECRET})
    assert resp.status_code == 503
    assert start_stack.calls == []


# ---------------------------------------------------------------------------
# Funnel stays paused
# ---------------------------------------------------------------------------
def test_get_booking_remains_blocked(client):
    resp = client.get("/booking")
    assert resp.status_code in (301, 302)
    assert "/booking-paused" in (resp.headers.get("Location") or "")


def test_post_confirm_booking_remains_blocked(client):
    resp = client.post("/confirm-booking", json={})
    assert resp.status_code == 403
    assert "temporarily disabled" in resp.get_json()["error"]


def test_ordinary_get_complete_payment_remains_blocked(client):
    resp = client.get("/complete-payment")
    assert resp.status_code in (301, 302)
    assert "/booking-paused" in (resp.headers.get("Location") or "")


def test_ordinary_post_api_complete_payment_remains_blocked(client):
    resp = client.post(
        "/api/complete-payment",
        json={"payment_session_token": RAW_TOKEN, "dataKey": DATA_KEY},
    )
    assert resp.status_code == 403
    assert "temporarily disabled" in resp.get_json()["error"]


# ---------------------------------------------------------------------------
# Successful start POST
# ---------------------------------------------------------------------------
def test_successful_post_creates_one_qa_session(client, start_stack, caplog):
    with caplog.at_level(logging.DEBUG):
        resp = client.post(START_PATH, data={"secret": TEST_SECRET})
    assert resp.status_code == 200
    assert len(start_stack.calls) == 1
    html = resp.get_data(as_text=True)
    token = _handoff_token(html)
    assert token
    assert "gml_payment_session_token" in html
    assert "<iframe" not in html.lower()
    assert "HPPtoken" not in html
    cookie = _cookie_value(resp)
    assert cookie
    header = _cookie_header(resp)
    assert "HttpOnly" in header
    assert "Secure" in header
    assert "SameSite=Strict" in header
    assert TEST_SECRET not in cookie
    assert TEST_SECRET not in html
    _assert_no_sensitive(html)
    _assert_no_sensitive(cookie)
    _assert_no_sensitive(caplog.text, extra=(token,))
    args, kwargs = start_stack.calls[0]
    guest = args[4]
    assert guest["first_name"] == "PRODUCTION"
    assert guest["last_name"] == "QA VALIDATION"
    assert args[5] == QA_SPECIAL_REQUESTS
    assert kwargs["session_token_hash"] == hash_payment_session_token(token)
    cap = parse_capability(cookie)
    assert cap is not None
    assert cap.session_token_hash == hash_payment_session_token(token)


def test_one_post_does_not_loop_create(client, start_stack):
    client.post(START_PATH, data={"secret": TEST_SECRET})
    client.post(START_PATH, data={"secret": TEST_SECRET})
    assert len(start_stack.calls) == 2


def test_select_qa_stay_uses_availability_and_lead_window():
    today = date(2026, 8, 24)
    seen = []

    def validate(check_in, check_out, rooms_req):
        seen.append((check_in, check_out, rooms_req[0]["name"]))
        nights = (check_out - check_in).days
        lead = (check_in - today).days
        if nights == 1 and lead == 72 and "Non-Smoking" in rooms_req[0]["name"]:
            return {"valid": True, "nights": 1, "subtotal": 1, "gst": 0, "atl": 0, "grand_total": 1}, 200
        return {"valid": False}, 409

    found = select_qa_stay(list(main.ROOM_CATALOG), validate, today=today)
    assert found is not None
    check_in, check_out, rooms_req, result = found
    assert (check_in - today).days == 72
    assert (check_out - check_in).days == 1
    assert result["valid"] is True
    assert "Non-Smoking" in rooms_req[0]["name"]
    assert all((ci - today).days >= 60 for ci, _co, _name in seen)
    assert all((ci - today).days <= 90 for ci, _co, _name in seen)


def test_select_qa_stay_fails_closed_when_nothing_available():
    found = select_qa_stay(list(main.ROOM_CATALOG), _unavailable_itinerary, today=date(2026, 8, 24))
    assert found is None


# ---------------------------------------------------------------------------
# Capability binding
# ---------------------------------------------------------------------------
def test_malformed_and_expired_capability_rejected(monkeypatch):
    monkeypatch.setenv("PAYMENT_PROD_VALIDATION_TEST_SECRET", TEST_SECRET)
    digest = hash_payment_session_token(RAW_TOKEN)
    fresh = mint_capability(digest)
    assert parse_capability(fresh) is not None
    assert parse_capability("not-a-capability") is None
    assert parse_capability(fresh[:-2] + "ab") is None
    expired = mint_capability(
        digest,
        now=datetime.now(timezone.utc) - timedelta(seconds=prod_test.CAPABILITY_TTL_SECONDS + 30),
    )
    assert parse_capability(expired) is None
    parsed = parse_capability(fresh)
    assert TEST_SECRET not in fresh
    assert parsed.session_token_hash == digest


def test_capability_hmac_binds_one_session(monkeypatch):
    monkeypatch.setenv("PAYMENT_PROD_VALIDATION_TEST_SECRET", TEST_SECRET)
    left = mint_capability(hash_payment_session_token(RAW_TOKEN))
    cap = parse_capability(left)
    other_hash = hash_payment_session_token(OTHER_TOKEN)
    parts = left.split(".")
    tampered = ".".join([parts[0], parts[1], other_hash, parts[3], parts[4]])
    assert parse_capability(tampered) is None
    assert cap.session_token_hash != other_hash


def test_valid_capability_permits_only_complete_payment_routes(
    client, monkeypatch
):
    _apply_env(monkeypatch, _production_moneris_env())
    _set_capability(client, RAW_TOKEN, monkeypatch)
    page = client.get("/complete-payment")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "complete_payment.html" not in html
    assert "complete_payment_ht.js" in html
    assert "Card required to secure your reservation" in html
    assert PRODUCTION_HOSTED_TOKENIZATION_URL in html
    for path in ("/booking", "/booker_contact", "/final_details"):
        resp = client.get(path)
        assert resp.status_code in (301, 302), path
        assert "/booking-paused" in (resp.headers.get("Location") or "")
    confirm = client.post("/confirm-booking", json={})
    assert confirm.status_code == 403


def test_capability_cannot_be_used_with_different_payment_session_token(
    client, monkeypatch
):
    _apply_env(monkeypatch, _production_moneris_env())
    monkeypatch.setattr(main, "_supabase_required", lambda: (True, None))
    complete = Mock(side_effect=AssertionError("complete_pending_payment must not run"))
    parse = Mock(side_effect=AssertionError("parse_browser_payment_request must not run"))
    monkeypatch.setattr(main, "complete_pending_payment", complete)
    monkeypatch.setattr(main, "parse_browser_payment_request", parse)
    persist = Mock(side_effect=AssertionError("must not create another reservation"))
    monkeypatch.setattr(main, "_persist_booking", persist)
    _set_capability(client, RAW_TOKEN, monkeypatch)
    resp = client.post(
        "/api/complete-payment",
        json={"payment_session_token": OTHER_TOKEN, "dataKey": DATA_KEY},
    )
    assert resp.status_code == 403
    complete.assert_not_called()
    parse.assert_not_called()
    persist.assert_not_called()


def test_matching_capability_calls_existing_completion(client, monkeypatch):
    _apply_env(monkeypatch, _production_moneris_env())
    monkeypatch.setattr(main, "_supabase_required", lambda: (True, None))
    persist = Mock()
    monkeypatch.setattr(main, "_persist_booking", persist)
    captured = {}

    def fake_complete(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "booking_reference": "BK-OK",
            "redirect_url": "/reservation-confirmation/BK-OK?token=t",
            "email_sent": True,
        }

    complete = Mock(side_effect=fake_complete)
    monkeypatch.setattr(main, "complete_pending_payment", complete)
    _set_capability(client, RAW_TOKEN, monkeypatch)
    resp = client.post(
        "/api/complete-payment",
        json={"payment_session_token": RAW_TOKEN, "dataKey": DATA_KEY},
    )
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True
    assert captured["payment_session_token"] == RAW_TOKEN
    assert captured["data_key"] == DATA_KEY
    persist.assert_not_called()
    header = _cookie_header(resp)
    assert COOKIE_NAME in header
    assert "Max-Age=0" in header or "max-age=0" in header.lower()
    refresh = client.get("/complete-payment")
    assert refresh.status_code in (301, 302)
    assert "/booking-paused" in (refresh.headers.get("Location") or "")
    assert complete.call_count == 1
    persist.assert_not_called()


def test_retry_payment_keeps_same_capability_and_does_not_create_reservation(
    client, monkeypatch
):
    _apply_env(monkeypatch, _production_moneris_env())
    monkeypatch.setattr(main, "_supabase_required", lambda: (True, None))
    persist = Mock()
    monkeypatch.setattr(main, "_persist_booking", persist)

    complete = Mock(
        side_effect=PaymentCompletionError("declined", status=422, retry_payment=True)
    )
    monkeypatch.setattr(main, "complete_pending_payment", complete)
    cookie_value = _set_capability(client, RAW_TOKEN, monkeypatch)
    resp = client.post(
        "/api/complete-payment",
        json={"payment_session_token": RAW_TOKEN, "dataKey": DATA_KEY},
    )
    assert resp.status_code == 422
    body = resp.get_json()
    assert body["retry_payment"] is True
    persist.assert_not_called()
    header = _cookie_header(resp)
    assert "Max-Age=0" not in header
    assert "max-age=0" not in header.lower()
    assert parse_capability(cookie_value) is not None
    refresh = client.get("/complete-payment")
    assert refresh.status_code == 200
    assert "complete_payment_ht.js" in refresh.get_data(as_text=True)
    assert complete.call_count == 1
    persist.assert_not_called()


def test_ambiguous_failure_clears_capability_so_refresh_cannot_retokenize(
    client, monkeypatch
):
    _apply_env(monkeypatch, _production_moneris_env())
    monkeypatch.setattr(main, "_supabase_required", lambda: (True, None))
    persist = Mock()
    monkeypatch.setattr(main, "_persist_booking", persist)
    complete = Mock(
        side_effect=PaymentCompletionError("RECONCILIATION_REQUIRED", status=502)
    )
    monkeypatch.setattr(main, "complete_pending_payment", complete)
    _set_capability(client, RAW_TOKEN, monkeypatch)

    resp = client.post(
        "/api/complete-payment",
        json={"payment_session_token": RAW_TOKEN, "dataKey": DATA_KEY},
    )
    assert resp.status_code == 502
    body = resp.get_json()
    assert body == {"success": False, "error": "RECONCILIATION_REQUIRED"}
    assert "retry_payment" not in body
    header = _cookie_header(resp)
    assert COOKIE_NAME in header
    assert "Max-Age=0" in header or "max-age=0" in header.lower()
    persist.assert_not_called()
    assert complete.call_count == 1

    refresh = client.get("/complete-payment")
    assert refresh.status_code in (301, 302)
    assert "/booking-paused" in (refresh.headers.get("Location") or "")
    assert complete.call_count == 1
    persist.assert_not_called()
    html = (WEBSITE_ROOT / "templates" / "complete_payment.html").read_text(encoding="utf-8")
    assert "Do not enter your card again" in html


def test_non_retryable_conflict_clears_capability(client, monkeypatch):
    _apply_env(monkeypatch, _production_moneris_env())
    monkeypatch.setattr(main, "_supabase_required", lambda: (True, None))
    persist = Mock()
    monkeypatch.setattr(main, "_persist_booking", persist)
    complete = Mock(
        side_effect=PaymentCompletionError(
            "This reservation cannot be completed.", status=409
        )
    )
    monkeypatch.setattr(main, "complete_pending_payment", complete)
    _set_capability(client, RAW_TOKEN, monkeypatch)

    resp = client.post(
        "/api/complete-payment",
        json={"payment_session_token": RAW_TOKEN, "dataKey": DATA_KEY},
    )
    assert resp.status_code == 409
    body = resp.get_json()
    assert "retry_payment" not in body
    header = _cookie_header(resp)
    assert COOKIE_NAME in header
    assert "Max-Age=0" in header or "max-age=0" in header.lower()
    refresh = client.get("/complete-payment")
    assert refresh.status_code in (301, 302)
    assert "/booking-paused" in (refresh.headers.get("Location") or "")
    assert complete.call_count == 1
    persist.assert_not_called()


def test_retryable_parse_error_keeps_capability(client, monkeypatch):
    _apply_env(monkeypatch, _production_moneris_env())
    monkeypatch.setattr(main, "_supabase_required", lambda: (True, None))
    persist = Mock()
    monkeypatch.setattr(main, "_persist_booking", persist)
    complete = Mock(side_effect=AssertionError("complete_pending_payment must not run"))
    monkeypatch.setattr(main, "complete_pending_payment", complete)
    _set_capability(client, RAW_TOKEN, monkeypatch)

    resp = client.post(
        "/api/complete-payment",
        json={"payment_session_token": RAW_TOKEN, "dataKey": "short"},
    )
    assert resp.status_code == 400
    assert resp.get_json().get("retry_payment") is True
    header = _cookie_header(resp)
    assert "Max-Age=0" not in header
    assert "max-age=0" not in header.lower()
    complete.assert_not_called()
    persist.assert_not_called()
    refresh = client.get("/complete-payment")
    assert refresh.status_code == 200
    persist.assert_not_called()


def test_paused_api_without_capability_does_not_call_moneris(client, monkeypatch):
    complete = Mock(side_effect=AssertionError("complete_pending_payment must not run"))
    parse = Mock(side_effect=AssertionError("parse_browser_payment_request must not run"))
    monkeypatch.setattr(main, "complete_pending_payment", complete)
    monkeypatch.setattr(main, "parse_browser_payment_request", parse)
    resp = client.post(
        "/api/complete-payment",
        json={"payment_session_token": RAW_TOKEN, "dataKey": DATA_KEY},
    )
    assert resp.status_code == 403
    complete.assert_not_called()
    parse.assert_not_called()
