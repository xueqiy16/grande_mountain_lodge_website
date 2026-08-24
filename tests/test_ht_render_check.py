"""TEMPORARY Hosted Tokenization production render-check route.

Auth uses PAYMENT_RECONCILIATION_ADMIN_SECRET via POST body only.
Does not unpause bookings, tokenize, create sessions, or call Moneris/Supabase.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
import requests

import main
from payment_api.config import (
    PRODUCTION_HOSTED_TOKENIZATION_URL,
    SANDBOX_HOSTED_TOKENIZATION_URL,
)
from payment_reconciliation import (
    ADMIN_SECRET_ENV,
    CRON_SECRET_ENV,
    MIN_ADMIN_SECRET_LENGTH,
)


WEBSITE_ROOT = Path(__file__).resolve().parents[1]
AUTH_TEMPLATE = WEBSITE_ROOT / "templates" / "ht_render_check_auth.html"
CHECK_TEMPLATE = WEBSITE_ROOT / "templates" / "ht_render_check.html"
PATH = "/internal/ht-render-check"

ADMIN_SECRET = "a" * MIN_ADMIN_SECRET_LENGTH
CRON_SECRET = "c" * MIN_ADMIN_SECRET_LENGTH
OTHER_SECRET = "d" * MIN_ADMIN_SECRET_LENGTH
HT_PROFILE = "ht-profile-test-id"
CLIENT_SECRET = "unique-moneris-client-secret-xyz"
PRODUCTION_ORIGIN = "https://www3.moneris.com"


FORBIDDEN_CHECK_MARKERS = (
    "tokenize",
    "postMessage",
    "dataKey",
    "/api/complete-payment",
    "complete_payment_ht.js",
    "gml_payment_session_token",
    "sessionStorage",
    "localStorage",
    "create_public_booking",
    "confirm-booking",
    "supabase",
    "payment_session_token",
    "tokenize-card",
    ADMIN_SECRET,
    CLIENT_SECRET,
)


def _forbid_network(monkeypatch):
    def fail(*_args, **_kwargs):
        raise AssertionError("network request is forbidden in HT render-check tests")

    monkeypatch.setattr(requests, "request", fail)
    monkeypatch.setattr(requests, "post", fail)
    monkeypatch.setattr(requests, "get", fail)
    monkeypatch.setattr(requests, "put", fail)
    monkeypatch.setattr(requests, "patch", fail)
    monkeypatch.setattr(requests, "delete", fail)


def _production_ht_env(monkeypatch):
    monkeypatch.setenv("MONERIS_ENV", "production")
    monkeypatch.setenv(
        "MONERIS_HOSTED_TOKENIZATION_URL", PRODUCTION_HOSTED_TOKENIZATION_URL
    )
    monkeypatch.setenv("MONERIS_HOSTED_TOKENIZATION_PROFILE_ID", HT_PROFILE)
    monkeypatch.setenv("MONERIS_CLIENT_SECRET", CLIENT_SECRET)


@pytest.fixture
def client(monkeypatch):
    _forbid_network(monkeypatch)
    main.app.config.update(TESTING=True)
    main.limiter.enabled = False
    try:
        yield main.app.test_client()
    finally:
        main.limiter.enabled = True


@pytest.fixture
def admin_secret(monkeypatch):
    monkeypatch.setenv(ADMIN_SECRET_ENV, ADMIN_SECRET)
    monkeypatch.setenv(CRON_SECRET_ENV, CRON_SECRET)


def test_direct_bookings_remain_paused():
    assert main.DIRECT_BOOKINGS_PAUSED is True
    src = (WEBSITE_ROOT / "main.py").read_text(encoding="utf-8")
    assert "DIRECT_BOOKINGS_PAUSED = True" in src
    assert "DIRECT_BOOKINGS_PAUSED = False" not in src


def test_route_is_marked_temporary():
    src = inspect.getsource(main.ht_render_check)
    assert "TEMPORARY" in src
    assert "load_hosted_tokenization_browser_config" in src
    assert "_booking_funnel_blocked" not in src
    assert "authorize_reconciliation_admin_posted_secret" in src
    assert "request.form.get(\"secret\")" in src
    assert "request.args" not in src
    assert "set_cookie" not in src.lower()


def test_get_shows_auth_form_not_iframe(client, admin_secret):
    assert main.DIRECT_BOOKINGS_PAUSED is True
    resp = client.get(PATH)
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'method="post"' in html.lower()
    assert 'name="secret"' in html
    assert 'type="password"' in html
    assert "<iframe" not in html.lower()
    assert "monerisFrame" not in html
    assert PRODUCTION_HOSTED_TOKENIZATION_URL not in html
    assert SANDBOX_HOSTED_TOKENIZATION_URL not in html
    assert HT_PROFILE not in html
    assert ADMIN_SECRET not in html
    assert "PAYMENT_RECONCILIATION_ADMIN_SECRET" not in html
    assert resp.headers.get("Set-Cookie") is None
    assert resp.headers.get("Content-Security-Policy") is None


def test_get_ignores_query_string_secret(client, admin_secret):
    resp = client.get(f"{PATH}?secret={ADMIN_SECRET}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "<iframe" not in html.lower()
    assert ADMIN_SECRET not in html
    assert 'name="secret"' in html


def test_get_works_while_paused(client, admin_secret):
    assert main.DIRECT_BOOKINGS_PAUSED is True
    paused = client.get("/complete-payment")
    assert paused.status_code in (301, 302)
    assert "/booking-paused" in (paused.headers.get("Location") or "")
    resp = client.get(PATH)
    assert resp.status_code == 200
    assert "/booking-paused" not in (resp.headers.get("Location") or "")


def test_wrong_post_secret_is_401(client, admin_secret, monkeypatch):
    _production_ht_env(monkeypatch)
    resp = client.post(PATH, data={"secret": OTHER_SECRET})
    assert resp.status_code == 401
    body = resp.get_data(as_text=True)
    assert "Unauthorized" in body
    assert "<iframe" not in body.lower()
    assert OTHER_SECRET not in body
    assert ADMIN_SECRET not in body
    assert HT_PROFILE not in body
    assert PRODUCTION_HOSTED_TOKENIZATION_URL not in body
    assert resp.headers.get("Set-Cookie") is None


def test_missing_post_secret_is_401(client, admin_secret, monkeypatch):
    _production_ht_env(monkeypatch)
    resp = client.post(PATH, data={})
    assert resp.status_code == 401
    assert "<iframe" not in resp.get_data(as_text=True).lower()


def test_query_string_secret_does_not_authorize_post(
    client, admin_secret, monkeypatch
):
    _production_ht_env(monkeypatch)
    resp = client.post(f"{PATH}?secret={ADMIN_SECRET}", data={})
    assert resp.status_code == 401
    assert "<iframe" not in resp.get_data(as_text=True).lower()


def test_bearer_header_does_not_authorize_post(client, admin_secret, monkeypatch):
    _production_ht_env(monkeypatch)
    resp = client.post(
        PATH,
        data={},
        headers={"Authorization": f"Bearer {ADMIN_SECRET}"},
    )
    assert resp.status_code == 401


def test_expiry_cron_secret_cannot_authorize(client, admin_secret, monkeypatch):
    _production_ht_env(monkeypatch)
    resp = client.post(PATH, data={"secret": CRON_SECRET})
    assert resp.status_code == 401


def test_missing_admin_secret_is_unavailable(client, monkeypatch):
    monkeypatch.delenv(ADMIN_SECRET_ENV, raising=False)
    _production_ht_env(monkeypatch)
    resp = client.post(PATH, data={"secret": ADMIN_SECRET})
    assert resp.status_code == 503
    body = resp.get_data(as_text=True)
    assert "unavailable" in body.lower()
    assert ADMIN_SECRET not in body
    assert HT_PROFILE not in body
    assert "<iframe" not in body.lower()


def test_short_admin_secret_is_unavailable(client, monkeypatch):
    monkeypatch.setenv(ADMIN_SECRET_ENV, "s" * 31)
    _production_ht_env(monkeypatch)
    resp = client.post(PATH, data={"secret": "s" * 31})
    assert resp.status_code == 503
    assert "s" * 31 not in resp.get_data(as_text=True)


def test_correct_post_secret_renders_iframe_only(
    client, admin_secret, monkeypatch
):
    assert main.DIRECT_BOOKINGS_PAUSED is True
    _production_ht_env(monkeypatch)
    resp = client.post(PATH, data={"secret": ADMIN_SECRET})
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Hosted Tokenization production render check" in html
    assert "<iframe" in html.lower()
    assert 'id="monerisFrame"' in html
    assert PRODUCTION_HOSTED_TOKENIZATION_URL in html
    assert SANDBOX_HOSTED_TOKENIZATION_URL not in html
    assert "width:220px" not in html
    assert "pan_label=" in html
    assert "display_labels=1" in html
    assert "enable_cc_formatting=1" in html
    assert "enable_exp_formatting=1" in html
    assert resp.headers.get("Content-Security-Policy") == (
        f"frame-src {PRODUCTION_ORIGIN}"
    )
    assert resp.headers.get("Set-Cookie") is None
    assert ADMIN_SECRET not in html
    assert "PAYMENT_RECONCILIATION_ADMIN_SECRET" not in html
    assert CLIENT_SECRET not in html
    for marker in FORBIDDEN_CHECK_MARKERS:
        assert marker not in html
    assert "<script" not in html.lower()
    assert "<form" not in html.lower()
    assert "name=\"secret\"" not in html


def test_iframe_page_has_no_payment_or_booking_behavior():
    html = CHECK_TEMPLATE.read_text(encoding="utf-8")
    auth = AUTH_TEMPLATE.read_text(encoding="utf-8")
    combined = html + auth
    for marker in (
        "postMessage",
        "dataKey",
        "/api/complete-payment",
        "complete_payment_ht.js",
        "gml_payment_session_token",
        "sessionStorage",
        "localStorage",
        "create_public_booking",
        "confirm-booking",
        "supabase",
        "tokenize-card",
        "addEventListener",
    ):
        assert marker not in combined
    assert "TEMPORARY" in html
    assert "TEMPORARY" in auth
    assert "<script" not in html.lower()


def test_invalid_ht_config_is_safe_503(client, admin_secret, monkeypatch, caplog):
    monkeypatch.setenv("MONERIS_ENV", "production")
    monkeypatch.setenv(
        "MONERIS_HOSTED_TOKENIZATION_URL", SANDBOX_HOSTED_TOKENIZATION_URL
    )
    monkeypatch.setenv("MONERIS_HOSTED_TOKENIZATION_PROFILE_ID", HT_PROFILE)
    resp = client.post(PATH, data={"secret": ADMIN_SECRET})
    assert resp.status_code == 503
    body = resp.get_data(as_text=True)
    assert "unavailable" in body.lower()
    assert HT_PROFILE not in body
    assert ADMIN_SECRET not in body
    assert SANDBOX_HOSTED_TOKENIZATION_URL not in body
    assert PRODUCTION_HOSTED_TOKENIZATION_URL not in body
    assert "<iframe" not in body.lower()
    assert HT_PROFILE not in caplog.text
    assert ADMIN_SECRET not in caplog.text
    assert CLIENT_SECRET not in caplog.text
