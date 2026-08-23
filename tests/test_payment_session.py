"""Payment-session helpers and pending-booking browser contract.

These tests prove internal identifiers never leak into JSON, redirect URLs,
templates, logs, or emails. They do not execute SQL or talk to Supabase.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, timedelta
from pathlib import Path

import pytest

import main
import payment_session
from flask import render_template
from payment_session import (
    BROWSER_CREATE_KEYS,
    generate_payment_session_token,
    hash_payment_session_token,
    is_session_token_hash,
    leaked_internal_keys,
    pending_payment_browser_payload,
    payment_session_token_for_browser,
    server_create_booking_result,
    to_browser_booking_create_response,
)
from postgrest.exceptions import APIError


WEBSITE_ROOT = Path(__file__).resolve().parents[1]

CANONICAL = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
RESERVATION = "11111111-2222-3333-4444-555555555555"
PAYMENT_SESSION = "99999999-8888-7777-6666-555555555555"
SESSION_HASH = "ab" * 32
MONERIS_PM = "pm_leak_test_id"
MONERIS_ISSUER = "issuer_leak_test_id"
DATA_KEY = "dataKey_leak_test"
CONFIRMATION_TOKEN = "confirmation-token-must-not-leak"
RAW_TOKEN = "raw-payment-session-token-value"

SENTINELS = (
    CANONICAL,
    RESERVATION,
    PAYMENT_SESSION,
    SESSION_HASH,
    MONERIS_PM,
    MONERIS_ISSUER,
    DATA_KEY,
    CONFIRMATION_TOKEN,
)

INTERNAL_RPC_PAYLOAD = {
    "ok": True,
    "booking_reference": "BK-ABC123",
    "confirmation_token": CONFIRMATION_TOKEN,
    "canonical_booking_id": CANONICAL,
    "reservation_id": RESERVATION,
    "payment_session_id": PAYMENT_SESSION,
    "session_token_hash": SESSION_HASH,
    "session_status": "OPEN",
    "token_rotated": True,
    "reused": False,
    "paymentMethodId": MONERIS_PM,
    "issuerId": MONERIS_ISSUER,
    "dataKey": DATA_KEY,
}

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

FORBIDDEN_TEMPLATE_IDENTIFIERS = (
    "canonical_booking_id",
    "reservation_id",
    "payment_session_id",
    "session_token_hash",
    "paymentMethodId",
    "issuerId",
    "moneris_payment_method_id",
    "moneris_issuer_id",
    "claim_id",
    "current_registration_idempotency_key",
    "registration_idempotency_key",
)


def _assert_no_sentinels(text, extra=()):
    blob = text if isinstance(text, str) else json.dumps(text, default=str)
    for sentinel in SENTINELS + tuple(extra):
        assert sentinel not in blob, f"leaked sentinel {sentinel!r}"


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def test_direct_bookings_remain_paused():
    assert main.DIRECT_BOOKINGS_PAUSED is True


def test_generate_token_is_urlsafe_32():
    token = generate_payment_session_token()
    assert isinstance(token, str)
    assert len(token) >= 32
    other = generate_payment_session_token()
    assert token != other


def test_hash_is_sha256_hex():
    token = generate_payment_session_token()
    digest = hash_payment_session_token(token)
    assert digest == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert is_session_token_hash(digest)
    assert digest != token


def test_default_contract_is_live_v6(monkeypatch):
    monkeypatch.delenv("CREATE_PUBLIC_BOOKING_CONTRACT", raising=False)
    assert payment_session.booking_rpc_contract() == "live_v6"
    assert payment_session.uses_pending_payment_rpc() is False


def test_blank_contract_is_live_v6(monkeypatch):
    monkeypatch.setenv("CREATE_PUBLIC_BOOKING_CONTRACT", "   ")
    assert payment_session.booking_rpc_contract() == "live_v6"


def test_exact_live_v6_contract(monkeypatch):
    monkeypatch.setenv("CREATE_PUBLIC_BOOKING_CONTRACT", "live_v6")
    assert payment_session.booking_rpc_contract() == "live_v6"
    assert payment_session.uses_pending_payment_rpc() is False


def test_pending_v7_contract(monkeypatch):
    monkeypatch.setenv("CREATE_PUBLIC_BOOKING_CONTRACT", "pending_v7")
    assert payment_session.uses_pending_payment_rpc() is True


@pytest.mark.parametrize("value", ("v7", "pending", "PENDING_V7", "live-v6", "typo"))
def test_unknown_contract_is_rejected(monkeypatch, value):
    monkeypatch.setenv("CREATE_PUBLIC_BOOKING_CONTRACT", value)
    with pytest.raises(payment_session.BookingRpcContractError):
        payment_session.booking_rpc_contract()


def test_unknown_contract_does_not_call_create_public_booking(
    client, unpaused, monkeypatch
):
    monkeypatch.setenv("CREATE_PUBLIC_BOOKING_CONTRACT", "pending")
    calls = []
    monkeypatch.setattr(
        main, "_persist_booking", lambda *a, **k: calls.append("persist") or {"ok": True}
    )
    monkeypatch.setattr(
        main, "_generate_booking_reference", lambda: calls.append("ref") or "BK-1"
    )
    resp = client.post("/confirm-booking", json=_booking_json())
    assert resp.status_code == 503
    assert calls == []
    body = resp.get_json()
    assert body["success"] is False
    assert "canonical_booking_id" not in body


def test_live_v6_rpc_args_omit_session_hash():
    args = main._create_public_booking_rpc_args(
        "idem", "BK-1", "conf", {"email": "a@b.c"}, [], "c" * 64, session_token_hash="a" * 64
    )
    assert "p_session_token_hash" not in args
    assert set(args) == {
        "p_idempotency_key",
        "p_booking_reference",
        "p_confirmation_token",
        "p_guest",
        "p_bookings",
        "p_cancellation_token_hash",
    }


def test_pending_v7_rpc_args_include_hash_not_raw_token(monkeypatch):
    monkeypatch.setenv("CREATE_PUBLIC_BOOKING_CONTRACT", "pending_v7")
    raw = generate_payment_session_token()
    digest = hash_payment_session_token(raw)
    args = main._create_public_booking_rpc_args(
        "idem", "BK-1", "conf", {"email": "a@b.c"}, [], "c" * 64, session_token_hash=digest
    )
    assert args["p_session_token_hash"] == digest
    assert raw not in json.dumps(args)


def test_server_create_result_drops_internal_ids():
    out = server_create_booking_result(INTERNAL_RPC_PAYLOAD)
    assert out["ok"] is True
    assert out["booking_reference"] == "BK-ABC123"
    assert out["reused"] is False
    assert out["token_rotated"] is True
    assert set(out) <= {
        "ok",
        "booking_reference",
        "confirmation_token",
        "reused",
        "token_rotated",
    }
    _assert_no_sentinels(
        {k: v for k, v in out.items() if k != "confirmation_token"},
        extra=SENTINELS,
    )
    for key in (
        "canonical_booking_id",
        "reservation_id",
        "payment_session_id",
        "session_token_hash",
        "paymentMethodId",
        "issuerId",
    ):
        assert key not in out


def test_browser_payload_omits_token_when_not_rotated():
    body = to_browser_booking_create_response(
        {
            **INTERNAL_RPC_PAYLOAD,
            "ok": True,
            "booking_reference": "BK-ABC123",
            "reused": True,
            "token_rotated": False,
        },
        raw_payment_session_token=RAW_TOKEN,
    )
    assert body["success"] is True
    assert body["reused"] is True
    assert "payment_session_token" not in body
    assert leaked_internal_keys(body) == set()
    _assert_no_sentinels(body, extra=(RAW_TOKEN,))


def test_browser_payload_includes_token_only_when_rotated():
    assert payment_session_token_for_browser(
        token_rotated=True, raw_token=RAW_TOKEN
    ) == RAW_TOKEN
    assert payment_session_token_for_browser(
        token_rotated=False, raw_token=RAW_TOKEN
    ) is None
    body = pending_payment_browser_payload(
        INTERNAL_RPC_PAYLOAD,
        raw_payment_session_token=RAW_TOKEN,
        nights=2,
        subtotal=200,
        gst=10,
        atl=8,
        grand_total=218,
    )
    assert set(body) <= BROWSER_CREATE_KEYS
    assert body["next_step"] == "payment"
    assert body["payment_url"] == "/complete-payment"
    assert "?" not in body["payment_url"]
    assert body["payment_session_token"] == RAW_TOKEN
    assert leaked_internal_keys(body) == set()
    _assert_no_sentinels(body, extra=SENTINELS)


class _RpcExec:
    def __init__(self, owner):
        self.owner = owner

    def execute(self):
        if self.owner.exc:
            raise self.owner.exc
        return type("Res", (), {"data": self.owner.data})()


class FakeSupabase:
    def __init__(self, data=None, exc=None):
        self.data = data if data is not None else dict(INTERNAL_RPC_PAYLOAD)
        self.exc = exc
        self.rpc_calls = []

    def rpc(self, name, args):
        self.rpc_calls.append((name, args))
        return _RpcExec(self)


def test_persist_filters_rpc_and_does_not_fallback(monkeypatch):
    monkeypatch.setenv("CREATE_PUBLIC_BOOKING_CONTRACT", "pending_v7")
    fake = FakeSupabase()
    monkeypatch.setattr(main, "supabase", fake)
    monkeypatch.setattr(
        main,
        "_assign_physical_rooms",
        lambda *a, **k: (
            [{"room_id": "room-1", "line_total": 200.0, "rate": 100.0}],
            None,
        ),
    )
    digest = hash_payment_session_token(generate_payment_session_token())
    out = main._persist_booking(
        date.today() + timedelta(days=10),
        date.today() + timedelta(days=12),
        ITINERARY,
        [{"adults": 2, "children": 0, "pets": 0}],
        GUEST,
        None,
        "BK-ABC123",
        CONFIRMATION_TOKEN,
        "idem-" + "a" * 32,
        "c" * 64,
        session_token_hash=digest,
    )
    assert len(fake.rpc_calls) == 1
    name, args = fake.rpc_calls[0]
    assert name == "create_public_booking"
    assert args["p_session_token_hash"] == digest
    assert "canonical_booking_id" not in out
    assert "reservation_id" not in out
    assert "payment_session_id" not in out
    _assert_no_sentinels(
        {k: v for k, v in out.items() if k != "confirmation_token"}
    )


def test_persist_live_v6_omits_seventh_arg(monkeypatch):
    monkeypatch.delenv("CREATE_PUBLIC_BOOKING_CONTRACT", raising=False)
    fake = FakeSupabase(
        data={
            "ok": True,
            "booking_reference": "BK-ABC123",
            "confirmation_token": CONFIRMATION_TOKEN,
            "reused": False,
        }
    )
    monkeypatch.setattr(main, "supabase", fake)
    monkeypatch.setattr(
        main,
        "_assign_physical_rooms",
        lambda *a, **k: (
            [{"room_id": "room-1", "line_total": 200.0, "rate": 100.0}],
            None,
        ),
    )
    main._persist_booking(
        date.today() + timedelta(days=10),
        date.today() + timedelta(days=12),
        ITINERARY,
        [{"adults": 2, "children": 0, "pets": 0}],
        GUEST,
        None,
        "BK-ABC123",
        CONFIRMATION_TOKEN,
        "idem-" + "a" * 32,
        "c" * 64,
        session_token_hash="a" * 64,
    )
    assert "p_session_token_hash" not in fake.rpc_calls[0][1]


def test_unknown_contract_persist_does_not_rpc(monkeypatch):
    monkeypatch.setenv("CREATE_PUBLIC_BOOKING_CONTRACT", "pending")
    fake = FakeSupabase()
    monkeypatch.setattr(main, "supabase", fake)
    monkeypatch.setattr(
        main,
        "_assign_physical_rooms",
        lambda *a, **k: (
            [{"room_id": "room-1", "line_total": 200.0, "rate": 100.0}],
            None,
        ),
    )
    out = main._persist_booking(
        date.today() + timedelta(days=10),
        date.today() + timedelta(days=12),
        ITINERARY,
        [{"adults": 2, "children": 0, "pets": 0}],
        GUEST,
        None,
        "BK-ABC123",
        CONFIRMATION_TOKEN,
        "idem-" + "a" * 32,
        "c" * 64,
        session_token_hash="a" * 64,
    )
    assert out["ok"] is False
    assert fake.rpc_calls == []


def test_persist_failure_logs_omit_internal_ids(monkeypatch, caplog):
    monkeypatch.setenv("CREATE_PUBLIC_BOOKING_CONTRACT", "pending_v7")
    message = (
        f"create_public_booking failed canonical_booking_id={CANONICAL} "
        f"reservation_id={RESERVATION} payment_session_id={PAYMENT_SESSION} "
        f"session_token_hash={SESSION_HASH}"
    )
    fake = FakeSupabase(exc=RuntimeError(message))
    monkeypatch.setattr(main, "supabase", fake)
    monkeypatch.setattr(
        main,
        "_assign_physical_rooms",
        lambda *a, **k: (
            [{"room_id": "room-1", "line_total": 200.0, "rate": 100.0}],
            None,
        ),
    )
    with caplog.at_level(logging.ERROR, logger=main.logger.name):
        out = main._persist_booking(
            date.today() + timedelta(days=10),
            date.today() + timedelta(days=12),
            ITINERARY,
            [{"adults": 2, "children": 0, "pets": 0}],
            GUEST,
            None,
            "BK-ABC123",
            CONFIRMATION_TOKEN,
            "idem-" + "a" * 32,
            "c" * 64,
            session_token_hash="a" * 64,
        )
    assert out["ok"] is False
    assert out["error"] == "Could not store your booking. Please try again."
    assert "type=RuntimeError" in caplog.text
    _assert_no_sentinels(caplog.text)
    assert "canonical_booking_id" not in caplog.text
    assert SESSION_HASH not in caplog.text
    assert CONFIRMATION_TOKEN not in caplog.text
    assert GUEST["email"] not in caplog.text


def test_safe_postgrest_fields_are_allowlisted_only():
    exc = APIError(
        {
            "code": "PGRST202",
            "message": "Could not find the function public.create_public_booking",
            "details": "Searched for the function with parameters p_session_token_hash",
            "hint": "Perhaps you meant to call the 6-argument overload",
            "email": GUEST["email"],
            "payment_session_token": RAW_TOKEN,
            "session_token_hash": SESSION_HASH,
            "confirmation_token": CONFIRMATION_TOKEN,
            "cancellation_token": "cancel-token-must-not-leak",
            "dataKey": DATA_KEY,
            "paymentMethodId": MONERIS_PM,
            "issuerId": MONERIS_ISSUER,
            "canonical_booking_id": CANONICAL,
            "guest": dict(GUEST),
        }
    )
    fields = main._safe_postgrest_error_fields(exc)
    assert set(fields) == {"code", "message", "details", "hint"}
    assert fields["code"] == "PGRST202"
    assert "create_public_booking" in fields["message"]
    assert "p_session_token_hash" in fields["details"]
    assert "6-argument" in fields["hint"]
    blob = json.dumps(fields)
    _assert_no_sentinels(blob)
    assert GUEST["email"] not in blob
    assert RAW_TOKEN not in blob
    assert CONFIRMATION_TOKEN not in blob


def test_safe_postgrest_fields_skip_non_text_and_do_not_use_str_exc():
    class Weird:
        code = {"nested": RAW_TOKEN}
        message = ["payload", GUEST]
        details = {"dataKey": DATA_KEY}
        hint = True

        def __str__(self):
            return (
                f"{GUEST['email']} {RAW_TOKEN} {SESSION_HASH} "
                f"{CONFIRMATION_TOKEN} {DATA_KEY}"
            )

    fields = main._safe_postgrest_error_fields(Weird())
    assert fields == {
        "code": None,
        "message": None,
        "details": None,
        "hint": None,
    }


def test_persist_apierror_logs_safe_postgrest_metadata(monkeypatch, caplog):
    monkeypatch.setenv("CREATE_PUBLIC_BOOKING_CONTRACT", "pending_v7")
    digest = hash_payment_session_token(generate_payment_session_token())
    exc = APIError(
        {
            "code": "PGRST202",
            "message": (
                "Could not find the function public.create_public_booking "
                "without a matching signature in the schema cache"
            ),
            "details": (
                "Searched for the function public.create_public_booking with "
                "parameters p_idempotency_key, p_booking_reference, "
                "p_confirmation_token, p_guest, p_bookings, "
                "p_cancellation_token_hash, p_session_token_hash"
            ),
            "hint": (
                "Try a different schema or check the function name and "
                "parameter names in the OpenAPI spec"
            ),
            "email": GUEST["email"],
            "payment_session_token": RAW_TOKEN,
            "session_token_hash": digest,
            "dataKey": DATA_KEY,
            "paymentMethodId": MONERIS_PM,
            "issuerId": MONERIS_ISSUER,
            "canonical_booking_id": CANONICAL,
        }
    )
    fake = FakeSupabase(exc=exc)
    monkeypatch.setattr(main, "supabase", fake)
    monkeypatch.setattr(
        main,
        "_assign_physical_rooms",
        lambda *a, **k: (
            [{"room_id": "room-1", "line_total": 200.0, "rate": 100.0}],
            None,
        ),
    )
    with caplog.at_level(logging.ERROR, logger=main.logger.name):
        out = main._persist_booking(
            date.today() + timedelta(days=10),
            date.today() + timedelta(days=12),
            ITINERARY,
            [{"adults": 2, "children": 0, "pets": 0}],
            GUEST,
            None,
            "BK-ABC123",
            CONFIRMATION_TOKEN,
            "idem-" + "a" * 32,
            "c" * 64,
            session_token_hash=digest,
        )
    assert out["ok"] is False
    assert out["error"] == "Could not store your booking. Please try again."
    text = caplog.text
    assert "type=APIError" in text
    assert "code=PGRST202" in text
    assert "message=Could not find the function public.create_public_booking" in text
    assert "p_session_token_hash" in text
    assert "schema cache" in text
    assert GUEST["email"] not in text
    assert GUEST["phone"] not in text
    assert GUEST["first_name"] not in text
    assert RAW_TOKEN not in text
    assert digest not in text
    assert CONFIRMATION_TOKEN not in text
    assert DATA_KEY not in text
    assert MONERIS_PM not in text
    assert MONERIS_ISSUER not in text
    _assert_no_sentinels(text)
    assert fake.rpc_calls and fake.rpc_calls[0][1]["p_guest"]["email"] == GUEST["email"]


def test_confirm_booking_apierror_browser_stays_generic(
    client, unpaused, monkeypatch, caplog
):
    monkeypatch.setenv("CREATE_PUBLIC_BOOKING_CONTRACT", "pending_v7")
    fake = FakeSupabase(
        exc=APIError(
            {
                "code": "42883",
                "message": "function public.create_public_booking does not exist",
                "details": None,
                "hint": "No function matches the given name and argument types",
                "email": GUEST["email"],
                "dataKey": DATA_KEY,
            }
        )
    )
    monkeypatch.setattr(main, "supabase", fake)
    monkeypatch.setattr(main, "_supabase_required", lambda: (True, None))
    monkeypatch.setattr(
        main, "_validate_itinerary", lambda *a, **k: (ITINERARY, 200)
    )
    monkeypatch.setattr(main, "_generate_booking_reference", lambda: "BK-ABC123")
    monkeypatch.setattr(
        main,
        "_assign_physical_rooms",
        lambda *a, **k: (
            [{"room_id": "room-1", "line_total": 200.0, "rate": 100.0}],
            None,
        ),
    )
    with caplog.at_level(logging.ERROR, logger=main.logger.name):
        resp = client.post("/confirm-booking", json=_booking_json())
    assert resp.status_code == 500
    body = resp.get_json()
    assert body["success"] is False
    assert body["error"] == "Could not store your booking. Please try again."
    raw = resp.get_data(as_text=True)
    assert "42883" not in raw
    assert "does not exist" not in raw
    assert GUEST["email"] not in raw
    _assert_no_sentinels(body)
    assert "code=42883" in caplog.text
    assert "function public.create_public_booking does not exist" in caplog.text
    assert GUEST["email"] not in caplog.text
    assert DATA_KEY not in caplog.text


def _patch_booking_flow(monkeypatch, persist_return, *, contract="pending_v7"):
    monkeypatch.setenv("CREATE_PUBLIC_BOOKING_CONTRACT", contract)
    monkeypatch.setattr(main, "_supabase_required", lambda: (True, None))
    monkeypatch.setattr(
        main, "_validate_itinerary", lambda *a, **k: (ITINERARY, 200)
    )
    monkeypatch.setattr(main, "_generate_booking_reference", lambda: "BK-ABC123")
    monkeypatch.setattr(main, "_persist_booking", persist_return)
    emails = []
    monkeypatch.setattr(
        main,
        "send_confirmation_email",
        lambda app, confirmation: emails.append(confirmation) or (True, None),
    )
    monkeypatch.setattr(
        main,
        "fetch_confirmation_from_supabase",
        lambda *a, **k: {
            "booking_reference": "BK-ABC123",
            "guest_email": GUEST["email"],
            "canonical_booking_id": CANONICAL,
        },
    )
    return emails


def test_pending_create_json_has_no_internal_ids(client, unpaused, monkeypatch):
    emails = _patch_booking_flow(
        monkeypatch,
        lambda *a, **k: {
            "ok": True,
            "booking_reference": "BK-ABC123",
            "confirmation_token": CONFIRMATION_TOKEN,
            "reused": False,
            "token_rotated": True,
            "canonical_booking_id": CANONICAL,
            "reservation_id": RESERVATION,
            "payment_session_id": PAYMENT_SESSION,
            "session_token_hash": SESSION_HASH,
            "paymentMethodId": MONERIS_PM,
            "issuerId": MONERIS_ISSUER,
        },
    )
    resp = client.post("/confirm-booking", json=_booking_json())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    assert body["booking_reference"] == "BK-ABC123"
    assert body["reused"] is False
    assert body["next_step"] == "payment"
    assert body["payment_url"] == "/complete-payment"
    assert "payment_session_token" in body
    assert "redirect_url" not in body
    assert "email_sent" not in body
    assert set(body) <= BROWSER_CREATE_KEYS
    assert leaked_internal_keys(body) == set()
    _assert_no_sentinels(body)
    _assert_no_sentinels(resp.get_data(as_text=True))
    assert emails == []


def test_pending_processing_replay_omits_new_token(client, unpaused, monkeypatch):
    emails = _patch_booking_flow(
        monkeypatch,
        lambda *a, **k: {
            "ok": True,
            "booking_reference": "BK-ABC123",
            "confirmation_token": CONFIRMATION_TOKEN,
            "reused": True,
            "token_rotated": False,
            "canonical_booking_id": CANONICAL,
            "reservation_id": RESERVATION,
            "payment_session_id": PAYMENT_SESSION,
        },
    )
    resp = client.post("/api/complete-booking", json=_booking_json())
    body = resp.get_json()
    assert body["reused"] is True
    assert "payment_session_token" not in body
    assert body["payment_url"] == "/complete-payment"
    _assert_no_sentinels(body)
    assert emails == []


def test_pending_path_does_not_send_email(client, unpaused, monkeypatch):
    emails = _patch_booking_flow(
        monkeypatch,
        lambda *a, **k: {
            "ok": True,
            "booking_reference": "BK-ABC123",
            "reused": False,
            "token_rotated": True,
        },
    )
    client.post("/confirm-booking", json=_booking_json())
    assert emails == []


def test_live_v6_still_emails_and_confirms(client, unpaused, monkeypatch):
    emails = _patch_booking_flow(
        monkeypatch,
        lambda *a, **k: {
            "ok": True,
            "booking_reference": "BK-ABC123",
            "confirmation_token": CONFIRMATION_TOKEN,
            "reused": False,
            "token_rotated": False,
            "canonical_booking_id": CANONICAL,
        },
        contract="live_v6",
    )
    resp = client.post("/confirm-booking", json=_booking_json())
    body = resp.get_json()
    assert body["success"] is True
    assert body["redirect_url"] == (
        f"/reservation-confirmation/BK-ABC123?token={CONFIRMATION_TOKEN}"
    )
    assert "next_step" not in body
    assert CANONICAL not in body["redirect_url"]
    assert RESERVATION not in (body.get("redirect_url") or "")
    assert "canonical_booking_id" not in body
    assert len(emails) == 1
    for sentinel in (CANONICAL, RESERVATION, PAYMENT_SESSION, SESSION_HASH, MONERIS_PM, MONERIS_ISSUER, DATA_KEY):
        assert sentinel not in json.dumps(body)


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


def test_complete_payment_template_has_no_internal_ids(client, unpaused, monkeypatch):
    monkeypatch.setenv("CREATE_PUBLIC_BOOKING_CONTRACT", "pending_v7")
    _sandbox_ht_env(monkeypatch)
    resp = client.get("/complete-payment")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Complete your payment" in html
    assert "var paymentEnabled = true" in html
    for ident in FORBIDDEN_TEMPLATE_IDENTIFIERS:
        assert ident not in html
    _assert_no_sentinels(html)
    assert "unique-moneris-client-secret-xyz" not in html
    assert "unique-moneris-client-id-xyz" not in html
    assert "unique-supabase-service-role-xyz" not in html
    assert "https://api.sb.moneris.io" not in html


def test_complete_payment_live_v6_does_not_enable_payment(client, unpaused, monkeypatch):
    monkeypatch.setenv("CREATE_PUBLIC_BOOKING_CONTRACT", "live_v6")
    resp = client.get("/complete-payment")
    assert resp.status_code == 404
    html = resp.get_data(as_text=True)
    assert "var paymentEnabled = false" in html
    assert "var htIframeSrc = \"\"" in html
    _assert_no_sentinels(html)


def test_complete_payment_ignores_internal_query_params(client, unpaused, monkeypatch):
    monkeypatch.setenv("CREATE_PUBLIC_BOOKING_CONTRACT", "pending_v7")
    _sandbox_ht_env(monkeypatch)
    resp = client.get(
        "/complete-payment"
        f"?canonical_booking_id={CANONICAL}"
        f"&reservation_id={RESERVATION}"
        f"&payment_session_id={PAYMENT_SESSION}"
        f"&session_token_hash={SESSION_HASH}"
        f"&paymentMethodId={MONERIS_PM}"
        f"&issuerId={MONERIS_ISSUER}"
    )
    html = resp.get_data(as_text=True)
    _assert_no_sentinels(html)


def test_complete_payment_redirect_while_paused_has_no_ids(client):
    assert main.DIRECT_BOOKINGS_PAUSED is True
    resp = client.get("/complete-payment")
    assert resp.status_code in (301, 302)
    location = resp.headers.get("Location") or ""
    assert "/booking-paused" in location
    _assert_no_sentinels(location)


def test_templates_do_not_reference_internal_ids():
    for path in (WEBSITE_ROOT / "templates").glob("*.html"):
        text = path.read_text(encoding="utf-8")
        for ident in FORBIDDEN_TEMPLATE_IDENTIFIERS:
            assert ident not in text, f"{path.name} contains {ident}"
    email_txt = (WEBSITE_ROOT / "templates" / "confirmation_email.txt").read_text(
        encoding="utf-8"
    )
    for ident in FORBIDDEN_TEMPLATE_IDENTIFIERS:
        assert ident not in email_txt


def test_rendered_email_and_confirmation_omit_internal_ids():
    poisoned = {
        "booking_reference": "BK-ABC123",
        "guest_name": "Ada Lovelace",
        "guest_email": "ada@example.com",
        "check_in": "January 1, 2027",
        "check_out": "January 3, 2027",
        "nights": 2,
        "grand_total": 218,
        "rooms": [
            {
                "room_type_name": "Queen",
                "room_number": "101",
                "guest_count": "2 adults",
            }
        ],
        "special_requests": "",
        "cancel_url": "https://grandemountainlodge.com/cancel-reservation/BK-ABC123?token=raw",
        "access_token": "page-access-token",
        "canonical_booking_id": CANONICAL,
        "reservation_id": RESERVATION,
        "payment_session_id": PAYMENT_SESSION,
        "session_token_hash": SESSION_HASH,
        "paymentMethodId": MONERIS_PM,
        "issuerId": MONERIS_ISSUER,
        "lodge": {
            "name": "Grande Mountain Lodge",
            "phone": "780-827-2007",
            "phone_tel": "7808272007",
            "email": "info@example.com",
            "address": "addr",
            "maps_url": "https://maps.example",
            "payment_methods": "card",
            "cheques_note": "no cheques",
            "check_in_hours": "3pm",
            "check_out_hours": "11am",
            "min_check_in_age": 18,
            "parking": "free",
        },
    }
    with main.app.app_context():
        html_email = render_template("confirmation_email.html", confirmation=poisoned)
        text_email = render_template("confirmation_email.txt", confirmation=poisoned)
        page = render_template(
            "reservation_confirmation.html", confirmation=poisoned, email_notice=None
        )
        pay = render_template("complete_payment.html")
    for blob in (html_email, text_email, page, pay):
        _assert_no_sentinels(blob)
        for ident in FORBIDDEN_TEMPLATE_IDENTIFIERS:
            assert ident not in blob
