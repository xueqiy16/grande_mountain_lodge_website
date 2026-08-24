"""Hosted Tokenization wiring for /complete-payment.

Static and config tests only. They do not load Moneris's iframe, call Card
Validation, or touch the network.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import requests

import main
import payment_completion
from payment_api.config import (
    PRODUCTION_HOSTED_TOKENIZATION_URL,
    SANDBOX_HOSTED_TOKENIZATION_URL,
)
from payment_ht import (
    HostedTokenizationConfigError,
    build_iframe_src,
    load_hosted_tokenization_browser_config,
)


WEBSITE_ROOT = Path(__file__).resolve().parents[1]
HT_JS = WEBSITE_ROOT / "static" / "complete_payment_ht.js"
TEMPLATE = WEBSITE_ROOT / "templates" / "complete_payment.html"

HT_PROFILE = "ht-profile-test-id"
CLIENT_SECRET = "unique-moneris-client-secret-xyz"
CLIENT_ID = "unique-moneris-client-id-xyz"
SERVICE_ROLE = "unique-supabase-service-role-xyz"
MERCHANT_ID = "unique-moneris-merchant-xyz"

SANDBOX_ORIGIN = "https://esqa.moneris.com"
PRODUCTION_ORIGIN = "https://www3.moneris.com"


def _forbid_network(monkeypatch):
    def fail(*_args, **_kwargs):
        raise AssertionError("network request is forbidden in HT tests")

    monkeypatch.setattr(requests, "request", fail)
    monkeypatch.setattr(requests, "post", fail)
    monkeypatch.setattr(requests, "get", fail)
    monkeypatch.setattr(requests, "put", fail)
    monkeypatch.setattr(requests, "patch", fail)
    monkeypatch.setattr(requests, "delete", fail)


def _sandbox_env(**overrides):
    env = {
        "MONERIS_ENV": "sandbox",
        "MONERIS_HOSTED_TOKENIZATION_URL": SANDBOX_HOSTED_TOKENIZATION_URL,
        "MONERIS_HOSTED_TOKENIZATION_PROFILE_ID": HT_PROFILE,
    }
    env.update(overrides)
    return env


def _production_env(**overrides):
    env = {
        "MONERIS_ENV": "production",
        "MONERIS_HOSTED_TOKENIZATION_URL": PRODUCTION_HOSTED_TOKENIZATION_URL,
        "MONERIS_HOSTED_TOKENIZATION_PROFILE_ID": HT_PROFILE,
    }
    env.update(overrides)
    return env


def _apply_env(monkeypatch, env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def client():
    main.app.config.update(TESTING=True)
    main.limiter.enabled = False
    try:
        yield main.app.test_client()
    finally:
        main.limiter.enabled = True


@pytest.fixture
def unpaused(monkeypatch):
    monkeypatch.setattr(main, "DIRECT_BOOKINGS_PAUSED", False)


@pytest.fixture
def pending_v7(monkeypatch):
    monkeypatch.setenv("CREATE_PUBLIC_BOOKING_CONTRACT", "pending_v7")


@pytest.fixture
def sandbox_ht(monkeypatch):
    _apply_env(
        monkeypatch,
        {
            **_sandbox_env(),
            "MONERIS_CLIENT_SECRET": CLIENT_SECRET,
            "MONERIS_CLIENT_ID": CLIENT_ID,
            "MONERIS_MERCHANT_ID": MERCHANT_ID,
            "SUPABASE_SERVICE_ROLE_KEY": SERVICE_ROLE,
            "MONERIS_API_BASE_URL": "https://api.sb.moneris.io",
        },
    )


def test_live_v6_complete_payment_unavailable(client, unpaused, monkeypatch):
    monkeypatch.setenv("CREATE_PUBLIC_BOOKING_CONTRACT", "live_v6")
    resp = client.get("/complete-payment")
    assert resp.status_code == 404
    html = resp.get_data(as_text=True)
    assert "var paymentEnabled = false" in html
    assert "var htIframeSrc = \"\"" in html
    assert SANDBOX_HOSTED_TOKENIZATION_URL not in html
    assert PRODUCTION_HOSTED_TOKENIZATION_URL not in html
    assert CLIENT_SECRET not in html


def test_invalid_contract_fails_closed(client, unpaused, monkeypatch):
    monkeypatch.setenv("CREATE_PUBLIC_BOOKING_CONTRACT", "pending")
    resp = client.get("/complete-payment")
    assert resp.status_code == 503
    html = resp.get_data(as_text=True)
    assert "var paymentEnabled = false" in html
    assert "var htIframeSrc = \"\"" in html


def test_missing_session_token_does_not_initialize_iframe():
    html = TEMPLATE.read_text(encoding="utf-8")
    token_idx = html.find('sessionStorage.getItem("gml_payment_session_token")')
    iframe_idx = html.find("createElement(\"iframe\")")
    assert token_idx != -1
    assert iframe_idx != -1
    assert token_idx < iframe_idx
    assert 'if (!token)' in html
    assert html.find("return;", token_idx) < iframe_idx
    assert "<iframe" not in html.lower()


def test_sandbox_renders_sandbox_ht_url_only(
    client, unpaused, pending_v7, sandbox_ht
):
    resp = client.get("/complete-payment")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert SANDBOX_HOSTED_TOKENIZATION_URL in html
    assert PRODUCTION_HOSTED_TOKENIZATION_URL not in html
    assert f'var htOrigin = "{SANDBOX_ORIGIN}"' in html
    assert "Card required to secure your reservation" in html
    assert "No charge or pre-authorization" in html
    assert "Credit Card Information" in html
    assert "Save card & confirm reservation" in html
    assert "#ff5778" in html
    assert "#D63683" in html
    assert 'button.textContent = "Complete payment"' not in html
    assert resp.headers.get("Content-Security-Policy") == f"frame-src {SANDBOX_ORIGIN}"


def test_production_renders_production_ht_url_only(
    client, unpaused, pending_v7, monkeypatch
):
    _apply_env(monkeypatch, _production_env())
    resp = client.get("/complete-payment")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert PRODUCTION_HOSTED_TOKENIZATION_URL in html
    assert SANDBOX_HOSTED_TOKENIZATION_URL not in html
    assert f'var htOrigin = "{PRODUCTION_ORIGIN}"' in html
    assert resp.headers.get("Content-Security-Policy") == (
        f"frame-src {PRODUCTION_ORIGIN}"
    )


def test_env_url_mismatch_fails_closed(client, unpaused, pending_v7, monkeypatch):
    _apply_env(
        monkeypatch,
        _sandbox_env(
            MONERIS_HOSTED_TOKENIZATION_URL=PRODUCTION_HOSTED_TOKENIZATION_URL
        ),
    )
    resp = client.get("/complete-payment")
    assert resp.status_code == 503
    html = resp.get_data(as_text=True)
    assert "var paymentEnabled = false" in html
    assert "var htIframeSrc = \"\"" in html
    assert HT_PROFILE not in html


def test_missing_ht_config_fails_closed_on_pending_v7(
    client, unpaused, pending_v7, monkeypatch
):
    monkeypatch.delenv("MONERIS_HOSTED_TOKENIZATION_URL", raising=False)
    monkeypatch.delenv("MONERIS_HOSTED_TOKENIZATION_PROFILE_ID", raising=False)
    monkeypatch.delenv("MONERIS_ENV", raising=False)
    resp = client.get("/complete-payment")
    assert resp.status_code == 503
    html = resp.get_data(as_text=True)
    assert "var paymentEnabled = false" in html


def test_no_secret_config_in_template(client, unpaused, pending_v7, sandbox_ht):
    resp = client.get("/complete-payment")
    html = resp.get_data(as_text=True)
    for secret in (CLIENT_SECRET, CLIENT_ID, SERVICE_ROLE, MERCHANT_ID):
        assert secret not in html
    assert "https://api.sb.moneris.io" not in html
    assert "https://api.moneris.io" not in html
    assert "MONERIS_CLIENT_SECRET" not in html
    assert "SUPABASE_SERVICE_ROLE_KEY" not in html


def test_iframe_query_uses_lodge_funnel_ht_params():
    src = build_iframe_src(SANDBOX_HOSTED_TOKENIZATION_URL, "abc&evil=1")
    parsed = urlparse(src)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == (
        SANDBOX_HOSTED_TOKENIZATION_URL
    )
    query = parse_qs(parsed.query)
    assert query["id"] == ["abc&evil=1"]
    assert "evil" not in query
    assert query["pmmsg"] == ["true"]
    assert query["enable_exp"] == ["1"]
    assert query["enable_cvd"] == ["1"]
    assert query["display_labels"] == ["1"]
    assert query["pan_label"] == ["Card number"]
    assert query["exp_label"] == ["Expiry (MM/YY)"]
    assert query["cvd_label"] == ["CVD"]
    assert query["enable_cc_formatting"] == ["1"]
    assert query["enable_exp_formatting"] == ["1"]
    css_textbox = query["css_textbox"][0]
    assert "width:220px" not in css_textbox
    assert "width:100%" in css_textbox
    assert "box-sizing:border-box" in css_textbox
    assert "css_textbox_pan" not in query
    assert "css_textbox_exp" not in query
    assert "css_textbox_cvd" not in query
    assert "css_input_label" in query


def test_load_config_does_not_rewrite_mismatch():
    with pytest.raises(HostedTokenizationConfigError) as ctx:
        load_hosted_tokenization_browser_config(
            _sandbox_env(
                MONERIS_HOSTED_TOKENIZATION_URL=PRODUCTION_HOSTED_TOKENIZATION_URL
            )
        )
    message = str(ctx.value)
    assert "mismatched" in message
    assert HT_PROFILE not in message


def test_template_security_invariants():
    html = TEMPLATE.read_text(encoding="utf-8")
    js = HT_JS.read_text(encoding="utf-8")
    combined = html + js
    lower = combined.lower()
    assert "console.log" not in lower
    assert "console.debug" not in lower
    assert "console.info" not in lower
    assert "console.warn" not in lower
    assert "console.error" not in lower
    assert "localstorage.setitem" not in lower
    assert "sessionstorage.setitem" not in lower
    assert "indexeddb" not in lower
    assert "document.cookie" not in lower
    assert 'postMessage("tokenize", "*")' not in html
    assert "postMessage('tokenize', '*')" not in html
    assert 'postMessage("tokenize", htOrigin)' in html
    assert "submitState !== \"idle\"" in html
    assert "retry_payment === true" in html
    assert "result.status === 400 || result.status === 422" not in html
    assert "beginSubmission(dataKey)" in html
    assert "submitMonerisDataKey(dataKey)" in html
    assert "sessionStorage.removeItem(\"gml_payment_session_token\")" in html
    success_idx = html.find("result.data.success")
    clear_idx = html.find('sessionStorage.removeItem("gml_payment_session_token")')
    assert success_idx != -1 and success_idx < clear_idx
    assert "innerHTML" not in html
    assert "setStatus(dataKey)" not in html
    assert "setStatus(payload.dataKey)" not in html
    assert "<input" not in html.lower()
    assert "eval(" not in combined
    assert "Card required to secure your reservation" in html
    assert "No charge or pre-authorization" in html
    assert "Credit Card Information" in html
    assert "Save card & confirm reservation" in html
    assert "#ff5778" in html
    assert "#D63683" in html
    assert 'button.textContent = "Complete payment"' not in html
    assert 'button.textContent = "Pay now"' not in html
    assert 'button.textContent = "Submit payment"' not in html
    assert 'postMessage("tokenize", htOrigin)' in html
    assert "payment_session_token: token" in html
    assert "dataKey: dataKey" in html


def test_direct_bookings_remain_paused():
    assert main.DIRECT_BOOKINGS_PAUSED is True


def test_paused_complete_payment_does_not_render_ht():
    assert main.DIRECT_BOOKINGS_PAUSED is True
    client = main.app.test_client()
    resp = client.get("/complete-payment")
    assert resp.status_code in (301, 302)
    assert "/booking-paused" in (resp.headers.get("Location") or "")


def test_payment_api_environ_is_allowlisted(monkeypatch):
    monkeypatch.setenv("MONERIS_ENV", "sandbox")
    monkeypatch.setenv("MONERIS_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setenv("MONERIS_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("SUPABASE_SECRET_KEY", SERVICE_ROLE)
    monkeypatch.setenv("ENABLE_QA_CARD_VALIDATION", "true")
    monkeypatch.setenv("QA_HT_ORIGIN", "https://evil.example")
    monkeypatch.setenv("ALLOWED_ADMIN_ORIGINS", "*")
    monkeypatch.setenv("MONERIS_ENABLED", "true")
    monkeypatch.setenv("NOT_A_PAYMENT_VAR", "nope")
    environ = payment_completion._payment_api_environ()
    assert set(environ) <= payment_completion.PAYMENT_API_ENV_NAMES
    assert "ENABLE_QA_CARD_VALIDATION" not in environ
    assert "QA_HT_ORIGIN" not in environ
    assert "ALLOWED_ADMIN_ORIGINS" not in environ
    assert "MONERIS_ENABLED" not in environ
    assert "NOT_A_PAYMENT_VAR" not in environ
    assert "SUPABASE_SECRET_KEY" not in environ
    assert environ["SUPABASE_SERVICE_ROLE_KEY"] == SERVICE_ROLE
    assert environ["MONERIS_CLIENT_SECRET"] == CLIENT_SECRET
    assert "PATH" not in environ


def test_ht_js_helpers_without_network(monkeypatch):
    _forbid_network(monkeypatch)
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available for HT helper tests")
    script = r"""
const ht = require('./static/complete_payment_ht.js');
const origin = 'https://esqa.moneris.com';
const source = {};
function assert(cond, msg) { if (!cond) { throw new Error(msg); } }

assert(ht.originAllowed(origin, origin), 'exact origin');
assert(!ht.originAllowed('https://evil.example', origin), 'wrong origin');
assert(!ht.originAllowed('https://esqa.moneris.com.', origin), 'suffix origin');
assert(ht.sourceAllowed(source, source), 'exact source');
assert(!ht.sourceAllowed({}, source), 'wrong source');
assert(ht.acceptMessage({origin: 'https://evil.example', source, data: '{}'}, origin, source) === null, 'reject origin');
assert(ht.acceptMessage({origin, source: {}, data: '{}'}, origin, source) === null, 'reject source');

const good = {responseCode: '001', dataKey: 'K'.repeat(26)};
assert(ht.extractDataKey(good, 25, 28) === 'K'.repeat(26), 'valid dataKey');
assert(ht.extractDataKey({responseCode: '001', dataKey: 'short'}, 25, 28) === null, 'short');
assert(ht.extractDataKey({responseCode: '001', dataKey: 'K'.repeat(29)}, 25, 28) === null, 'long');
assert(ht.extractDataKey({responseCode: '942', dataKey: 'K'.repeat(26)}, 25, 28) === null, 'fail code');
assert(ht.parseMessageData('not-json') === null, 'malformed');
assert(ht.parseMessageData('{"responseCode":"001"}').responseCode === '001', 'json');
assert(ht.acceptMessage({origin, source, data: 'nope'}, origin, source) === null, 'bad json via accept');
console.log('ht_helpers_ok');
"""
    result = subprocess.run(
        [node, "-e", script],
        cwd=WEBSITE_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "ht_helpers_ok" in result.stdout
    assert "K" * 26 not in result.stdout
