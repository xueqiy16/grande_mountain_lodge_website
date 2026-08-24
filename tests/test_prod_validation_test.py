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
    SAFE_PERSIST_DIAG_CODES,
    SAFE_UNAVAILABLE,
    START_PATH,
    mint_capability,
    parse_capability,
    persist_failure_user_message,
    require_production_api_and_ht_config,
    safe_persist_diag_code,
    select_qa_stay,
    server_persist_failure,
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
WEBSITE_SUPABASE_SECRET = "UNIQUE_WEBSITE_SUPABASE_SECRET_KEY_XYZ"
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
    def __init__(self, ok=True, persist_diag=None, error="Could not store your booking."):
        self.calls = []
        self.ok = ok
        self.persist_diag = persist_diag
        self.error = error

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if not self.ok:
            out = {"ok": False, "error": self.error}
            if self.persist_diag is not None:
                out["persist_diag"] = self.persist_diag
            return out
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
        WEBSITE_SUPABASE_SECRET,
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


def test_validation_request_contract_unchanged():
    src = VALIDATION_PY.read_text(encoding="utf-8")
    assert 'VALIDATIONS_PATH = "/validations"' in src
    assert "/payments" not in src
    assert 'STORE_PAYMENT_METHOD_MERCHANT_INITIATED = "MERCHANT_INITIATED"' in src
    assert 'COF_PAYMENT_INDICATOR_FIRST = "UNSCHEDULED_CREDENTIAL_ON_FILE"' in src
    assert 'COF_PAYMENT_INFORMATION_FIRST = "FIRST"' in src
    assert '"storePaymentMethod": STORE_PAYMENT_METHOD_MERCHANT_INITIATED' in src
    assert '"paymentIndicator": COF_PAYMENT_INDICATOR_FIRST' in src
    assert '"paymentInformation": COF_PAYMENT_INFORMATION_FIRST' in src
    assert "automaticCapture" not in src
    assert '"amount"' not in src
    assert "purchase" not in src.lower()


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
    assert "persist_failure_user_message" in temporary_route
    handle_booking = main_src[
        main_src.index("def handle_booking():") : main_src.index(
            "def _legacy_form_booking():"
        )
    ]
    assert "persist_failure_user_message" not in handle_booking
    assert "persist_diag" not in handle_booking
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
    body = resp.get_data(as_text=True)
    assert start_stack.calls == []
    assert TEST_SECRET not in body
    assert "PERSIST_" not in body


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


def test_preflight_reuses_payment_completion_environ_adapter():
    src = MODULE_PY.read_text(encoding="utf-8")
    assert "from payment_completion import _payment_api_environ" in src
    assert "load_config(_payment_api_environ())" in src
    assert "load_config()" not in src.replace("load_config(_payment_api_environ())", "")


def test_preflight_aliases_website_supabase_secret_key(
    client, start_stack, monkeypatch, caplog
):
    assert main.DIRECT_BOOKINGS_PAUSED is True
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    monkeypatch.setenv("SUPABASE_SECRET_KEY", WEBSITE_SUPABASE_SECRET)
    import payment_completion

    effective = payment_completion._payment_api_environ()
    assert "SUPABASE_SECRET_KEY" not in effective
    assert effective.get("SUPABASE_SERVICE_ROLE_KEY") == WEBSITE_SUPABASE_SECRET
    with caplog.at_level(logging.DEBUG):
        require_production_api_and_ht_config()
        resp = client.post(START_PATH, data={"secret": TEST_SECRET})
    assert resp.status_code == 200
    assert len(start_stack.calls) == 1
    _assert_no_sensitive(caplog.text)
    _assert_no_sensitive(resp.get_data(as_text=True))
    assert "Production card-validation test is unavailable." not in resp.get_data(
        as_text=True
    )


def test_preflight_fails_closed_without_supabase_server_credential(
    client, start_stack, monkeypatch, caplog
):
    assert main.DIRECT_BOOKINGS_PAUSED is True
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    with caplog.at_level(logging.ERROR):
        with pytest.raises(prod_test.ProdValidationTestError) as ctx:
            require_production_api_and_ht_config()
        resp = client.post(START_PATH, data={"secret": TEST_SECRET})
    assert ctx.value.status == 503
    assert str(ctx.value) == "Production card-validation test is unavailable."
    assert WEBSITE_SUPABASE_SECRET not in str(ctx.value)
    assert resp.status_code == 503
    assert start_stack.calls == []
    _assert_no_sensitive(caplog.text)
    _assert_no_sensitive(resp.get_data(as_text=True))
    assert "payment_api config invalid" in caplog.text
    assert WEBSITE_SUPABASE_SECRET not in caplog.text


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
    body = resp.get_json()
    assert "temporarily disabled" in body["error"]
    assert "persist_diag" not in body
    assert "PERSIST_" not in resp.get_data(as_text=True)


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


# ---------------------------------------------------------------------------
# TEMPORARY persist diagnostic (operator start route only)
# ---------------------------------------------------------------------------
class _PersistRpcExec:
    def __init__(self, owner):
        self.owner = owner

    def execute(self):
        if self.owner.exc:
            raise self.owner.exc
        return type("Res", (), {"data": self.owner.data})()


class _PersistFakeSupabase:
    def __init__(self, data=None, exc=None):
        self.data = data if data is not None else {
            "ok": True,
            "booking_reference": BOOKING_REF,
            "confirmation_token": "confirmation-token-must-not-leak",
            "reused": False,
            "token_rotated": True,
        }
        self.exc = exc
        self.rpc_calls = []

    def rpc(self, name, args):
        self.rpc_calls.append((name, args))
        return _PersistRpcExec(self)


def _run_persist(
    monkeypatch,
    *,
    assign_err=None,
    exc=None,
    data=None,
    session_hash=None,
    contract="pending_v7",
):
    monkeypatch.setenv("CREATE_PUBLIC_BOOKING_CONTRACT", contract)
    fake = _PersistFakeSupabase(data=data, exc=exc)
    monkeypatch.setattr(main, "supabase", fake)
    if assign_err is not None:
        monkeypatch.setattr(
            main, "_assign_physical_rooms", lambda *a, **k: (None, assign_err)
        )
    else:
        monkeypatch.setattr(
            main,
            "_assign_physical_rooms",
            lambda *a, **k: (
                [{"room_id": "room-leak-id", "line_total": 89.99, "rate": 89.99}],
                None,
            ),
        )
    digest = session_hash if session_hash is not None else hash_payment_session_token(
        RAW_TOKEN
    )
    itinerary = {
        "valid": True,
        "nights": 1,
        "subtotal": 89.99,
        "gst": 4.5,
        "atl": 5.4,
        "grand_total": 99.89,
        "rooms": [{"name": "Studio Double Queen Non-Smoking", "code": "STU-QQ-NS"}],
    }
    guest = prod_test.qa_guest_payload(QA_EMAIL)
    out = main._persist_booking(
        date(2026, 10, 23),
        date(2026, 10, 24),
        itinerary,
        [{"name": "Studio Double Queen Non-Smoking", "adults": 1, "children": 0, "pets": 0}],
        guest,
        QA_SPECIAL_REQUESTS,
        BOOKING_REF,
        "confirmation-token-must-not-leak",
        "idem-" + "a" * 32,
        "c" * 64,
        session_token_hash=digest,
    )
    return out, fake


_JUICY_RPC_ERROR = (
    f"column room_price does not exist email={QA_EMAIL} "
    f"booking_reference={BOOKING_REF} room_id=room-leak-id "
    f"canonical_booking_id={CANONICAL} reservation_id={RESERVATION} "
    f"payment_session_id={PAYMENT_SESSION} session_token_hash={'ab' * 32} "
    f"payment_session_token={RAW_TOKEN} cancellation_token_hash={'c' * 64} "
    f"idempotency_key=idem-{'a' * 32} dataKey={DATA_KEY} "
    "SELECT * FROM guests"
)


def test_server_persist_failure_rejects_unknown_diag():
    out = server_persist_failure("Could not store your booking.", "guest_email_conflict")
    assert out == {
        "ok": False,
        "error": "Could not store your booking.",
        "persist_diag": prod_test.PERSIST_OTHER,
    }
    assert persist_failure_user_message(out) == (
        f"{SAFE_UNAVAILABLE} [{prod_test.PERSIST_OTHER}]"
    )


def test_safe_persist_diag_ignores_error_text():
    persisted = {
        "ok": False,
        "error": _JUICY_RPC_ERROR,
        "persist_diag": "not-an-allowlisted-code",
    }
    assert safe_persist_diag_code(persisted) == prod_test.PERSIST_OTHER
    message = persist_failure_user_message(persisted)
    assert message == f"{SAFE_UNAVAILABLE} [{prod_test.PERSIST_OTHER}]"
    _assert_no_sensitive(message, extra=("room_price", "SELECT *", "room-leak-id", RAW_TOKEN))


@pytest.mark.parametrize(
    "exc_text, expected",
    [
        ("room_unavailable", prod_test.PERSIST_ROOM_UNAVAILABLE),
        (f"guest_email_conflict {QA_EMAIL}", prod_test.PERSIST_GUEST_EMAIL_CONFLICT),
        ("reservation_expired", prod_test.PERSIST_RESERVATION_EXPIRED),
        ("payment_session_stale_processing", prod_test.PERSIST_STALE_PROCESSING),
        ("reservation_state_inconsistent", prod_test.PERSIST_STATE_INCONSISTENT),
        (_JUICY_RPC_ERROR, prod_test.PERSIST_RPC_GENERIC),
    ],
)
def test_persist_booking_maps_known_rpc_failures(monkeypatch, caplog, exc_text, expected):
    with caplog.at_level(logging.ERROR):
        out, fake = _run_persist(monkeypatch, exc=RuntimeError(exc_text))
    assert out["ok"] is False
    assert out["persist_diag"] == expected
    assert fake.rpc_calls
    text = persist_failure_user_message(out)
    assert text == f"{SAFE_UNAVAILABLE} [{expected}]"
    _assert_no_sensitive(text, extra=(RAW_TOKEN, "room_price", "SELECT *", "room-leak-id"))
    _assert_no_sensitive(caplog.text, extra=(RAW_TOKEN, "room_price", "SELECT *"))
    assert QA_EMAIL not in text
    assert BOOKING_REF not in text
    assert CANONICAL not in text
    assert RESERVATION not in text
    assert PAYMENT_SESSION not in text
    assert "c" * 64 not in text
    assert DATA_KEY not in text


def test_persist_booking_assign_failed_diag(monkeypatch):
    assign_err = (
        "Room type Studio Double Queen Non-Smoking is not configured in the "
        "database. If Supabase credentials are set, verify SUPABASE_KEY is the "
        "service-role (secret) key from Project Settings → API, not the "
        "publishable key."
    )
    out, fake = _run_persist(monkeypatch, assign_err=assign_err)
    assert out["ok"] is False
    assert out["persist_diag"] == prod_test.PERSIST_ASSIGN_FAILED
    assert fake.rpc_calls == []
    text = persist_failure_user_message(out)
    assert text == f"{SAFE_UNAVAILABLE} [{prod_test.PERSIST_ASSIGN_FAILED}]"
    assert "SUPABASE_KEY" not in text
    assert "Studio Double Queen" not in text
    assert assign_err not in text


def test_persist_booking_missing_reference_diag(monkeypatch):
    out, _fake = _run_persist(monkeypatch, data={"ok": True})
    assert out["ok"] is False
    assert out["persist_diag"] == prod_test.PERSIST_NO_BOOKING_REFERENCE
    assert persist_failure_user_message(out) == (
        f"{SAFE_UNAVAILABLE} [{prod_test.PERSIST_NO_BOOKING_REFERENCE}]"
    )


def test_persist_booking_invalid_session_hash_is_other(monkeypatch):
    out, fake = _run_persist(monkeypatch, session_hash="not-a-session-hash")
    assert out["ok"] is False
    assert out["persist_diag"] == prod_test.PERSIST_OTHER
    assert fake.rpc_calls == []


@pytest.mark.parametrize("diag", sorted(SAFE_PERSIST_DIAG_CODES))
def test_temp_route_returns_allowlisted_persist_code(
    client, start_stack, monkeypatch, diag, caplog
):
    persist = PersistBox(ok=False, persist_diag=diag, error=_JUICY_RPC_ERROR)
    monkeypatch.setattr(main, "_persist_booking", persist)
    with caplog.at_level(logging.ERROR):
        resp = client.post(START_PATH, data={"secret": TEST_SECRET})
    assert resp.status_code == 503
    body = resp.get_data(as_text=True)
    assert body == f"{SAFE_UNAVAILABLE} [{diag}]"
    assert len(persist.calls) == 1
    _assert_no_sensitive(body, extra=(RAW_TOKEN, "room_price", "SELECT *", "room-leak-id"))
    _assert_no_sensitive(caplog.text, extra=(RAW_TOKEN, "room_price", "SELECT *"))
    assert "persist failed: " + diag in caplog.text


def test_temp_route_unknown_diag_becomes_other(client, start_stack, monkeypatch):
    persist = PersistBox(
        ok=False,
        persist_diag="guest_email_conflict",
        error=_JUICY_RPC_ERROR,
    )
    monkeypatch.setattr(main, "_persist_booking", persist)
    resp = client.post(START_PATH, data={"secret": TEST_SECRET})
    assert resp.status_code == 503
    assert resp.get_data(as_text=True) == (
        f"{SAFE_UNAVAILABLE} [{prod_test.PERSIST_OTHER}]"
    )
    _assert_no_sensitive(resp.get_data(as_text=True), extra=("room_price", RAW_TOKEN))


def test_temp_route_missing_diag_becomes_other(client, start_stack, monkeypatch):
    persist = PersistBox(ok=False, error=_JUICY_RPC_ERROR)
    monkeypatch.setattr(main, "_persist_booking", persist)
    resp = client.post(START_PATH, data={"secret": TEST_SECRET})
    assert resp.status_code == 503
    assert resp.get_data(as_text=True) == (
        f"{SAFE_UNAVAILABLE} [{prod_test.PERSIST_OTHER}]"
    )
    _assert_no_sensitive(resp.get_data(as_text=True), extra=("room_price", RAW_TOKEN))


def test_confirm_booking_does_not_expose_persist_diag(client, monkeypatch):
    monkeypatch.setattr(main, "DIRECT_BOOKINGS_PAUSED", False)
    monkeypatch.setattr(main, "_supabase_required", lambda: (True, None))
    monkeypatch.setattr(main, "_validate_itinerary", _valid_itinerary)
    monkeypatch.setattr(main, "_generate_booking_reference", lambda: BOOKING_REF)
    monkeypatch.setenv("CREATE_PUBLIC_BOOKING_CONTRACT", "pending_v7")
    monkeypatch.setenv("CANCELLATION_TOKEN_SECRET", CANCEL_SECRET)

    def persist(*_args, **_kwargs):
        return {
            "ok": False,
            "error": "Could not store your booking. Please try again.",
            "persist_diag": prod_test.PERSIST_RPC_GENERIC,
        }

    monkeypatch.setattr(main, "_persist_booking", persist)
    resp = client.post(
        "/confirm-booking",
        json={
            "checkin": "2026-10-23",
            "checkout": "2026-10-24",
            "rooms": [
                {
                    "name": "Studio Double Queen Non-Smoking",
                    "adults": 1,
                    "children": 0,
                    "pets": 0,
                }
            ],
            "guest": prod_test.qa_guest_payload(QA_EMAIL),
            "idempotency_key": "idem-" + "a" * 32,
        },
    )
    assert resp.status_code == 500
    body = resp.get_json()
    assert body == {
        "success": False,
        "error": "Could not store your booking. Please try again.",
    }
    raw = resp.get_data(as_text=True)
    assert "persist_diag" not in raw
    assert "PERSIST_RPC_GENERIC" not in raw
    assert "PERSIST_" not in raw
    _assert_no_sensitive(raw)
