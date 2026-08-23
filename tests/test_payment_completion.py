"""Server-side payment completion: claim, register, finalize.

These tests mock Supabase and register_booking_payment_credential. They do
not call live Moneris or execute sql/003.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import main
import payment_completion
from payment_completion import (
    ALLOWED_BROWSER_KEYS,
    BROWSER_COMPLETE_KEYS,
    PaymentCompletionError,
    complete_pending_payment,
    db_rpc_error_identifier,
    parse_browser_payment_request,
)
from payment_session import hash_payment_session_token, leaked_internal_keys
from postgrest.exceptions import APIError


CANONICAL = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
RESERVATION = "11111111-2222-3333-4444-555555555555"
PAYMENT_SESSION = "99999999-8888-7777-6666-555555555555"
CONFIRMATION_TOKEN = "confirmation-token-must-not-leak"
RAW_TOKEN = "B" * 43
DATA_KEY = "K" * 26
SESSION_HASH = hashlib.sha256(RAW_TOKEN.encode("utf-8")).hexdigest()
MONERIS_PM = "pm_leak_test_id"
MONERIS_ISSUER = "issuer_leak_test_id"
CLAIM_A = "c1a11111-1111-4111-a111-111111111111"
CLAIM_B = "c1b22222-2222-4222-b222-222222222222"
ATTEMPT_A = "a1111111-1111-4111-8111-111111111111"
ATTEMPT_B = "b2222222-2222-4222-8222-222222222222"

SENTINELS = (
    CANONICAL,
    RESERVATION,
    PAYMENT_SESSION,
    SESSION_HASH,
    DATA_KEY,
    CONFIRMATION_TOKEN,
    MONERIS_PM,
    MONERIS_ISSUER,
    RAW_TOKEN,
)


class CredentialPersistenceError(Exception):
    pass


class CredentialConflictError(Exception):
    pass


class CredentialRegistrationError(Exception):
    pass


class MonerisValidationError(Exception):
    CONFIRMED_DECLINE = "CONFIRMED_DECLINE"
    PROCESSOR_UNAVAILABLE = "PROCESSOR_UNAVAILABLE"

    def __init__(self, message, *, category):
        super().__init__(message)
        self.category = category


class Result:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    def __init__(self, owner):
        self.owner = owner

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        return Result(list(self.owner.bookings))


class FakeRpc:
    def __init__(self, owner, name, args):
        self.owner = owner
        self.name = name
        self.args = args

    def execute(self):
        if self.name == "claim_booking_payment_session":
            if self.owner.claim_exc:
                raise self.owner.claim_exc
            if self.owner.claim_stateful:
                return Result(self.owner._claim_session())
            return Result(self.owner.claim_result)
        if self.name == "finalize_booking_after_credential":
            if self.owner.finalize_exc:
                raise self.owner.finalize_exc
            if self.owner.email_row is None:
                self.owner.email_row = {
                    "delivery_status": "PENDING",
                    "claim_id": None,
                }
            return Result(self.owner.finalize_result)
        if self.name == "claim_reservation_confirmation_email":
            if self.owner.email_claim_exc:
                raise self.owner.email_claim_exc
            return Result(self.owner._claim_email())
        if self.name == "mark_reservation_confirmation_email_sent":
            if self.owner.mark_sent_exc:
                raise self.owner.mark_sent_exc
            return Result(self.owner._mark_email(self.args))
        if self.name == "release_reservation_confirmation_email_claim":
            if self.owner.release_exc:
                raise self.owner.release_exc
            return Result(self.owner._release_email(self.args))
        if self.name == "reopen_payment_session_after_failed_registration":
            if self.owner.reopen_exc:
                raise self.owner.reopen_exc
            return Result(self.owner._reopen(self.args))
        if self.name == "cancel_public_booking":
            self.owner.cancelled = True
            raise AssertionError("cancel_public_booking must not be called")
        raise AssertionError(f"unexpected rpc {self.name}")


class FakeSupabase:
    def __init__(self):
        self.order = []
        self.cancelled = False
        self.claim_exc = None
        self.finalize_exc = None
        self.email_claim_exc = None
        self.mark_sent_exc = None
        self.release_exc = None
        self.email_row = None
        self.email_sending_fresh = True
        self.next_claim_ids = []
        self.next_attempt_ids = [ATTEMPT_A]
        self.claim_stateful = False
        self.session_status = "PROCESSING"
        self.current_attempt_key = ATTEMPT_A
        self.credential_status = None
        self.reopen_exc = None
        self.expires_future = True
        self.reservation_uniform_pending = True
        self.bookings_status = "confirmed"
        self.claim_result = {
            "ok": True,
            "already_claimed": False,
            "already_finalized": False,
            "session_id": PAYMENT_SESSION,
            "reservation_id": RESERVATION,
            "canonical_booking_id": CANONICAL,
            "session_status": "PROCESSING",
            "current_registration_idempotency_key": ATTEMPT_A,
        }
        self.finalize_result = {
            "ok": True,
            "idempotent": False,
            "booking_reference": "BK-ABC123",
            "reservation_id": RESERVATION,
        }
        self.bookings = [
            {
                "booking_reference": "BK-ABC123",
                "confirmation_token": CONFIRMATION_TOKEN,
                "reservation_id": RESERVATION,
                "booking_status": "confirmed",
            }
        ]

    def _new_claim_id(self):
        if self.next_claim_ids:
            return self.next_claim_ids.pop(0)
        return str(uuid.uuid4())

    def _new_attempt_key(self):
        if self.next_attempt_ids:
            return self.next_attempt_ids.pop(0)
        return str(uuid.uuid4())

    def _claim_session(self):
        if self.session_status == "CONSUMED":
            return {
                "ok": True,
                "already_finalized": True,
                "session_id": PAYMENT_SESSION,
                "reservation_id": RESERVATION,
                "canonical_booking_id": CANONICAL,
                "session_status": "CONSUMED",
            }
        if self.session_status == "PROCESSING":
            return {
                "ok": True,
                "already_claimed": True,
                "session_id": PAYMENT_SESSION,
                "reservation_id": RESERVATION,
                "canonical_booking_id": CANONICAL,
                "session_status": "PROCESSING",
                "current_registration_idempotency_key": self.current_attempt_key,
            }
        key = self._new_attempt_key()
        self.current_attempt_key = key
        self.session_status = "PROCESSING"
        return {
            "ok": True,
            "already_claimed": False,
            "already_finalized": False,
            "session_id": PAYMENT_SESSION,
            "reservation_id": RESERVATION,
            "canonical_booking_id": CANONICAL,
            "session_status": "PROCESSING",
            "current_registration_idempotency_key": key,
        }

    def _reopen(self, args):
        presented = args.get("p_registration_idempotency_key")
        if self.session_status != "PROCESSING":
            raise APIError({"message": "payment_session_not_processing", "code": "P0001"})
        if presented is None or str(presented) != str(self.current_attempt_key):
            raise APIError({"message": "stale_registration_attempt", "code": "P0001"})
        if not self.expires_future:
            raise APIError({"message": "payment_session_expired", "code": "P0001"})
        if not self.reservation_uniform_pending:
            raise APIError({"message": "reservation_not_pending_payment", "code": "P0001"})
        if self.credential_status != "FAILED":
            raise APIError({"message": "registration_not_failed", "code": "P0001"})
        self.session_status = "OPEN"
        self.current_attempt_key = None
        self.claim_result["already_claimed"] = False
        self.claim_result["session_status"] = "OPEN"
        self.claim_result["current_registration_idempotency_key"] = None
        return {"ok": True, "session_status": "OPEN"}

    def _claim_email(self):
        row = self.email_row
        if row is None:
            raise RuntimeError("missing email row")
        if row.get("delivery_status") == "SENT":
            return {
                "ok": True,
                "should_send": False,
                "already_sent": True,
                "in_progress": False,
            }
        if row.get("delivery_status") == "SENDING" and self.email_sending_fresh:
            return {
                "ok": True,
                "should_send": False,
                "already_sent": False,
                "in_progress": True,
            }
        claim_id = self._new_claim_id()
        row["delivery_status"] = "SENDING"
        row["claim_id"] = claim_id
        return {
            "ok": True,
            "should_send": True,
            "already_sent": False,
            "in_progress": False,
            "claim_id": claim_id,
        }

    def _mark_email(self, args):
        row = self.email_row or {}
        presented = args.get("p_claim_id")
        if (
            row.get("delivery_status") != "SENDING"
            or presented is None
            or str(presented) != str(row.get("claim_id"))
        ):
            raise APIError({"message": "stale_email_claim", "code": "P0001"})
        row["delivery_status"] = "SENT"
        row["claim_id"] = None
        return {"ok": True, "already_sent": False}

    def _release_email(self, args):
        row = self.email_row or {}
        presented = args.get("p_claim_id")
        if (
            row.get("delivery_status") != "SENDING"
            or presented is None
            or str(presented) != str(row.get("claim_id"))
        ):
            raise APIError({"message": "stale_email_claim", "code": "P0001"})
        row["delivery_status"] = "PENDING"
        row["claim_id"] = None
        return {"ok": True}

    def rpc(self, name, args):
        self.order.append(("rpc", name, dict(args)))
        return FakeRpc(self, name, args)

    def table(self, name):
        self.order.append(("table", name, None))
        return FakeQuery(self)


class RegisterBox:
    def __init__(self):
        self.calls = []
        self.exc = None
        self.result = SimpleNamespace(registration_status="SUCCEEDED")

    def __call__(self, canonical_booking_id, data_key, idempotency_key):
        self.calls.append(
            {
                "canonical_booking_id": canonical_booking_id,
                "data_key": data_key,
                "idempotency_key": idempotency_key,
            }
        )
        if self.exc:
            raise self.exc
        return self.result


def _assert_no_sentinels(text, extra=()):
    blob = text if isinstance(text, str) else json.dumps(text, default=str)
    for sentinel in SENTINELS + tuple(extra):
        assert sentinel not in blob, f"leaked sentinel {sentinel!r}"


def _valid_body():
    return {"payment_session_token": RAW_TOKEN, "dataKey": DATA_KEY}


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
def completion_stack(monkeypatch, pending_v7):
    fake = FakeSupabase()
    register = RegisterBox()
    emails = []
    monkeypatch.setattr(main, "supabase", fake)
    monkeypatch.setattr(payment_completion, "register_credential_fn", register)
    monkeypatch.setattr(
        main,
        "send_confirmation_email",
        lambda app, confirmation: emails.append(confirmation) or (True, None),
    )
    monkeypatch.setattr(
        main,
        "fetch_confirmation_from_supabase",
        lambda supabase, ref, token=None: {
            "booking_reference": ref,
            "guest_email": "ada@example.com",
        },
    )
    return fake, register, emails


def _run_complete(fake, register, emails=None):
    emails = emails if emails is not None else []
    return complete_pending_payment(
        payment_session_token=RAW_TOKEN,
        data_key=DATA_KEY,
        supabase=fake,
        fetch_confirmation=lambda ref, tok: {
            "booking_reference": ref,
            "guest_email": "ada@example.com",
        },
        send_email=lambda confirmation: emails.append(confirmation) or (True, None),
        register_credential=register,
    )


def test_parse_accepts_only_token_and_datakey():
    token, data_key = parse_browser_payment_request(_valid_body())
    assert token == RAW_TOKEN
    assert data_key == DATA_KEY


@pytest.mark.parametrize(
    "payload",
    (
        {"payment_session_token": RAW_TOKEN},
        {"dataKey": DATA_KEY},
        {
            "payment_session_token": RAW_TOKEN,
            "dataKey": DATA_KEY,
            "booking_id": CANONICAL,
        },
        {
            "payment_session_token": RAW_TOKEN,
            "dataKey": DATA_KEY,
            "canonical_booking_id": CANONICAL,
        },
        {
            "payment_session_token": RAW_TOKEN,
            "dataKey": DATA_KEY,
            "reservation_id": RESERVATION,
        },
        {
            "payment_session_token": RAW_TOKEN,
            "dataKey": DATA_KEY,
            "payment_session_id": PAYMENT_SESSION,
        },
        {
            "payment_session_token": RAW_TOKEN,
            "dataKey": DATA_KEY,
            "booking_reference": "BK-ABC123",
        },
        {
            "payment_session_token": RAW_TOKEN,
            "dataKey": DATA_KEY,
            "confirmation_token": CONFIRMATION_TOKEN,
        },
        {
            "payment_session_token": RAW_TOKEN,
            "dataKey": DATA_KEY,
            "paymentMethodId": MONERIS_PM,
        },
        {
            "payment_session_token": RAW_TOKEN,
            "dataKey": DATA_KEY,
            "issuerId": MONERIS_ISSUER,
        },
        {
            "payment_session_token": RAW_TOKEN,
            "dataKey": DATA_KEY,
            "email_sent": True,
        },
        {
            "payment_session_token": RAW_TOKEN,
            "dataKey": DATA_KEY,
            "delivery_status": "SENT",
        },
        {
            "payment_session_token": RAW_TOKEN,
            "dataKey": DATA_KEY,
            "claim_id": CLAIM_A,
        },
        None,
        "token",
    ),
)
def test_parse_rejects_internal_or_incomplete_browser_fields(payload):
    with pytest.raises(PaymentCompletionError) as ctx:
        parse_browser_payment_request(payload)
    assert ctx.value.status == 400
    assert set(ALLOWED_BROWSER_KEYS) == {"payment_session_token", "dataKey"}


def test_payment_endpoint_unavailable_under_live_v6(
    client, unpaused, monkeypatch, completion_stack
):
    monkeypatch.setenv("CREATE_PUBLIC_BOOKING_CONTRACT", "live_v6")
    fake, register, emails = completion_stack
    resp = client.post("/api/complete-payment", json=_valid_body())
    assert resp.status_code == 404
    body = resp.get_json()
    assert body["success"] is False
    assert register.calls == []
    assert fake.order == []
    assert emails == []
    _assert_no_sentinels(body, extra=SENTINELS)


def test_unknown_contract_payment_endpoint_fail_closed(
    client, unpaused, monkeypatch, completion_stack
):
    monkeypatch.setenv("CREATE_PUBLIC_BOOKING_CONTRACT", "pending")
    fake, register, _emails = completion_stack
    resp = client.post("/api/complete-payment", json=_valid_body())
    assert resp.status_code == 503
    assert register.calls == []
    assert fake.order == []


def test_success_claim_register_finalize_email_order(pending_v7):
    fake = FakeSupabase()
    fake.next_claim_ids = [CLAIM_A]
    register = RegisterBox()
    emails = []
    body = _run_complete(fake, register, emails)
    names = [item[1] if item[0] == "rpc" else item[0] for item in fake.order]
    assert names[0] == "claim_booking_payment_session"
    assert register.calls
    claim_idx = next(
        i
        for i, item in enumerate(fake.order)
        if item[0] == "rpc" and item[1] == "claim_booking_payment_session"
    )
    finalize_idx = next(
        i
        for i, item in enumerate(fake.order)
        if item[0] == "rpc" and item[1] == "finalize_booking_after_credential"
    )
    assert claim_idx < finalize_idx
    # Claim RPC is recorded before register is invoked.
    assert fake.order[0][1] == "claim_booking_payment_session"
    assert names.count("finalize_booking_after_credential") == 1
    assert len(emails) == 1
    assert body["success"] is True
    assert set(body) <= BROWSER_COMPLETE_KEYS
    assert leaked_internal_keys(body) == set()
    assert CONFIRMATION_TOKEN in body["redirect_url"]
    assert body["redirect_url"].startswith(
        "/reservation-confirmation/BK-ABC123?token="
    )
    assert CANONICAL not in body["redirect_url"]
    assert RESERVATION not in json.dumps(body)
    assert DATA_KEY not in json.dumps(body)
    assert SESSION_HASH not in json.dumps(body)
    assert PAYMENT_SESSION not in json.dumps(body)
    assert ATTEMPT_A not in json.dumps(body)
    assert "current_registration_idempotency_key" not in json.dumps(body)
    assert CLAIM_A not in json.dumps(body)
    assert "claim_id" not in json.dumps(body)
    mark_args = next(
        item[2]
        for item in fake.order
        if item[0] == "rpc"
        and item[1] == "mark_reservation_confirmation_email_sent"
    )
    assert mark_args["p_claim_id"] == CLAIM_A
    assert mark_args["p_reservation_id"] == RESERVATION
    assert fake.cancelled is False


def test_raw_token_hashed_before_claim_and_not_sent(pending_v7):
    fake = FakeSupabase()
    register = RegisterBox()
    _run_complete(fake, register, [])
    name, args = fake.order[0][1], fake.order[0][2]
    assert name == "claim_booking_payment_session"
    assert args == {"p_session_token_hash": SESSION_HASH}
    dumped = json.dumps(fake.order)
    assert RAW_TOKEN not in dumped
    assert hash_payment_session_token(RAW_TOKEN) == SESSION_HASH


def test_session_id_and_canonical_come_only_from_claim(pending_v7):
    fake = FakeSupabase()
    register = RegisterBox()

    def guarded(canonical_booking_id, data_key, idempotency_key):
        assert fake.order and fake.order[0][1] == "claim_booking_payment_session"
        assert not any(
            item[1] == "finalize_booking_after_credential" for item in fake.order
        )
        return register(canonical_booking_id, data_key, idempotency_key)

    _run_complete(fake, guarded, [])
    assert register.calls[0]["canonical_booking_id"] == CANONICAL
    assert register.calls[0]["idempotency_key"] == ATTEMPT_A
    assert register.calls[0]["idempotency_key"] != PAYMENT_SESSION
    assert register.calls[0]["data_key"] == DATA_KEY
    finalize_args = next(
        item[2]
        for item in fake.order
        if item[0] == "rpc" and item[1] == "finalize_booking_after_credential"
    )
    assert finalize_args == {"p_session_id": PAYMENT_SESSION}


def test_already_processing_does_not_call_moneris(pending_v7):
    fake = FakeSupabase()
    fake.claim_result["already_claimed"] = True
    register = RegisterBox()
    with pytest.raises(PaymentCompletionError) as ctx:
        _run_complete(fake, register, [])
    assert ctx.value.status == 409
    assert register.calls == []
    assert not any(
        item[1] == "finalize_booking_after_credential" for item in fake.order
    )


def test_expired_session_does_not_call_moneris(pending_v7):
    fake = FakeSupabase()
    fake.claim_exc = APIError({"message": "payment_session_expired", "code": "P0001"})
    register = RegisterBox()
    with pytest.raises(PaymentCompletionError) as ctx:
        _run_complete(fake, register, [])
    assert ctx.value.status == 409
    assert register.calls == []


def test_stale_processing_does_not_call_moneris(pending_v7):
    fake = FakeSupabase()
    fake.claim_exc = APIError(
        {"message": "payment_session_stale_processing", "code": "P0001"}
    )
    register = RegisterBox()
    with pytest.raises(PaymentCompletionError) as ctx:
        _run_complete(fake, register, [])
    assert register.calls == []
    assert ctx.value.status == 409


def test_succeeded_finalizes_before_email(pending_v7):
    fake = FakeSupabase()
    register = RegisterBox()
    order = []

    def send_email(confirmation):
        order.append("email")
        return True, None

    def register_fn(*args, **kwargs):
        order.append("register")
        return register(*args, **kwargs)

    complete_pending_payment(
        payment_session_token=RAW_TOKEN,
        data_key=DATA_KEY,
        supabase=fake,
        fetch_confirmation=lambda ref, tok: {"booking_reference": ref},
        send_email=send_email,
        register_credential=register_fn,
    )
    rpc_names = [item[1] for item in fake.order if item[0] == "rpc"]
    assert rpc_names == [
        "claim_booking_payment_session",
        "finalize_booking_after_credential",
        "claim_reservation_confirmation_email",
        "mark_reservation_confirmation_email_sent",
    ]
    assert order == ["register", "email"]


def test_confirmation_token_only_after_finalize(pending_v7):
    fake = FakeSupabase()
    register = RegisterBox()
    emails = []
    try:
        fake.claim_exc = APIError(
            {"message": "payment_session_expired", "code": "P0001"}
        )
        _run_complete(fake, register, emails)
    except PaymentCompletionError as exc:
        assert CONFIRMATION_TOKEN not in exc.user_message
    body = None
    fake.claim_exc = None
    body = _run_complete(fake, register, emails)
    assert f"token={CONFIRMATION_TOKEN}" in body["redirect_url"]


def test_consumed_retry_does_not_duplicate_email(pending_v7):
    fake = FakeSupabase()
    register = RegisterBox()
    emails = []
    first = _run_complete(fake, register, emails)
    assert len(emails) == 1
    fake.claim_result = {
        "ok": True,
        "already_finalized": True,
        "session_id": PAYMENT_SESSION,
        "reservation_id": RESERVATION,
        "canonical_booking_id": CANONICAL,
        "session_status": "CONSUMED",
    }
    second = _run_complete(fake, register, emails)
    assert len(emails) == 1
    assert len(register.calls) == 1
    assert second["success"] is True
    assert second["email_sent"] is True
    assert first["redirect_url"] == second["redirect_url"]


def test_idempotent_finalize_does_not_duplicate_recorded_email(pending_v7):
    fake = FakeSupabase()
    fake.finalize_result["idempotent"] = True
    fake.email_row = {"delivery_status": "SENT"}
    register = RegisterBox()
    emails = []
    body = _run_complete(fake, register, emails)
    assert emails == []
    assert body["email_sent"] is True
    assert body["success"] is True


def test_reconciliation_required_does_not_cancel(pending_v7):
    fake = FakeSupabase()
    register = RegisterBox()
    register.result = SimpleNamespace(registration_status="RECONCILIATION_REQUIRED")
    with pytest.raises(PaymentCompletionError) as ctx:
        _run_complete(fake, register, [])
    assert ctx.value.status == 502
    assert fake.cancelled is False
    assert not any(
        item[1] == "finalize_booking_after_credential" for item in fake.order
    )
    assert not any(item[1] == "cancel_public_booking" for item in fake.order)


def test_persistence_error_does_not_cancel(pending_v7):
    fake = FakeSupabase()
    register = RegisterBox()
    register.exc = CredentialPersistenceError("persistence")
    with pytest.raises(PaymentCompletionError) as ctx:
        _run_complete(fake, register, [])
    assert ctx.value.status == 502
    assert fake.cancelled is False


def test_processor_unavailable_does_not_infer_from_message(pending_v7):
    fake = FakeSupabase()
    register = RegisterBox()
    register.exc = MonerisValidationError(
        "CONFIRMED_DECLINE in message should be ignored",
        category="PROCESSOR_UNAVAILABLE",
    )
    with pytest.raises(PaymentCompletionError) as ctx:
        _run_complete(fake, register, [])
    assert ctx.value.status == 502
    assert "CONFIRMED_DECLINE" not in ctx.value.user_message


def test_http_success_and_browser_ids_rejected(
    client, unpaused, pending_v7, completion_stack
):
    fake, register, emails = completion_stack
    resp = client.post("/api/complete-payment", json=_valid_body())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    assert set(body) <= BROWSER_COMPLETE_KEYS
    assert leaked_internal_keys(body) == set()
    assert "canonical_booking_id" not in body
    assert "payment_session_id" not in body
    assert DATA_KEY not in json.dumps(body)
    assert SESSION_HASH not in json.dumps(body)
    assert CANONICAL not in json.dumps(body)
    assert len(emails) == 1
    poisoned = dict(_valid_body())
    poisoned["canonical_booking_id"] = CANONICAL
    rejected = client.post("/api/complete-payment", json=poisoned)
    assert rejected.status_code == 400
    assert len(register.calls) == 1


def test_logs_omit_sensitive_values(pending_v7, caplog):
    fake = FakeSupabase()
    fake.claim_exc = APIError(
        {
            "message": "payment_session_expired",
            "code": "P0001",
            "details": (
                f"canonical={CANONICAL} reservation={RESERVATION} "
                f"session={PAYMENT_SESSION} hash={SESSION_HASH} "
                f"dataKey={DATA_KEY} token={RAW_TOKEN} "
                f"pm={MONERIS_PM} issuer={MONERIS_ISSUER}"
            ),
        }
    )
    register = RegisterBox()
    with caplog.at_level(logging.ERROR, logger=payment_completion.logger.name):
        with pytest.raises(PaymentCompletionError):
            _run_complete(fake, register, [])
    _assert_no_sentinels(caplog.text)


def test_complete_payment_js_clears_token_only_after_success():
    html = (
        Path(__file__).resolve().parents[1] / "templates" / "complete_payment.html"
    ).read_text(encoding="utf-8")
    success_idx = html.find("result.data.success")
    clear_idx = html.find('sessionStorage.removeItem("gml_payment_session_token")')
    assert success_idx != -1
    assert clear_idx != -1
    assert success_idx < clear_idx
    assert "sessionStorage.setItem" not in html
    assert "localStorage.setItem" not in html
    assert "dataKey" in html
    assert DATA_KEY not in html
    assert "submitMonerisDataKey" in html


def test_http_error_json_omits_internals(
    client, unpaused, pending_v7, completion_stack
):
    fake, register, emails = completion_stack
    fake.claim_exc = APIError(
        {
            "message": "payment_session_expired",
            "code": "P0001",
            "details": f"{CANONICAL} {DATA_KEY} {RAW_TOKEN}",
        }
    )
    resp = client.post("/api/complete-payment", json=_valid_body())
    assert resp.status_code == 409
    body = resp.get_json()
    assert body["success"] is False
    assert "redirect_url" not in body
    _assert_no_sentinels(body)
    assert emails == []
    assert register.calls == []


def _consumed_claim(fake):
    fake.claim_result = {
        "ok": True,
        "already_finalized": True,
        "session_id": PAYMENT_SESSION,
        "reservation_id": RESERVATION,
        "canonical_booking_id": CANONICAL,
        "session_status": "CONSUMED",
    }


def test_crash_after_finalize_before_email_claim_is_retryable(pending_v7):
    fake = FakeSupabase()
    register = RegisterBox()
    emails = []
    fake.email_claim_exc = RuntimeError("simulated crash before email claim")
    body = _run_complete(fake, register, emails)
    assert body["success"] is True
    assert "token=" in body["redirect_url"]
    assert emails == []
    assert fake.email_row["delivery_status"] == "PENDING"
    assert fake.bookings[0]["booking_status"] == "confirmed"
    fake.email_claim_exc = None
    _consumed_claim(fake)
    retry = _run_complete(fake, register, emails)
    assert len(emails) == 1
    assert retry["success"] is True
    assert retry["email_sent"] is True
    assert len(register.calls) == 1
    assert fake.email_row["delivery_status"] == "SENT"


def test_consumed_replay_recovers_unsent_email(pending_v7):
    fake = FakeSupabase()
    fake.email_row = {"delivery_status": "PENDING"}
    register = RegisterBox()
    emails = []
    _consumed_claim(fake)
    body = _run_complete(fake, register, emails)
    assert register.calls == []
    assert len(emails) == 1
    assert body["email_sent"] is True
    assert fake.email_row["delivery_status"] == "SENT"


def test_provider_failure_keeps_confirmed_and_retryable(pending_v7):
    fake = FakeSupabase()
    register = RegisterBox()
    emails = []

    def send_fail(confirmation):
        emails.append(confirmation)
        return False, "smtp failed"

    body = complete_pending_payment(
        payment_session_token=RAW_TOKEN,
        data_key=DATA_KEY,
        supabase=fake,
        fetch_confirmation=lambda ref, tok: {"booking_reference": ref},
        send_email=send_fail,
        register_credential=register,
    )
    assert body["success"] is True
    assert body["email_sent"] is False
    assert fake.email_row["delivery_status"] == "PENDING"
    assert fake.bookings[0]["booking_status"] == "confirmed"
    assert any(
        item[1] == "release_reservation_confirmation_email_claim"
        for item in fake.order
    )
    retry_emails = []
    _consumed_claim(fake)
    retry = complete_pending_payment(
        payment_session_token=RAW_TOKEN,
        data_key=DATA_KEY,
        supabase=fake,
        fetch_confirmation=lambda ref, tok: {"booking_reference": ref},
        send_email=lambda confirmation: retry_emails.append(confirmation) or (True, None),
        register_credential=register,
    )
    assert len(retry_emails) == 1
    assert retry["email_sent"] is True
    assert fake.email_row["delivery_status"] == "SENT"


def test_provider_success_mark_sent_failure_is_ambiguous(pending_v7):
    fake = FakeSupabase()
    fake.mark_sent_exc = RuntimeError("mark sent failed")
    register = RegisterBox()
    emails = []
    body = _run_complete(fake, register, emails)
    assert len(emails) == 1
    assert body["success"] is True
    assert body["email_sent"] is True
    assert fake.email_row["delivery_status"] == "SENDING"
    _consumed_claim(fake)
    fake.mark_sent_exc = None
    retry = _run_complete(fake, register, emails)
    assert len(emails) == 1
    assert retry["success"] is True
    assert retry["email_sent"] is False
    assert fake.email_row["delivery_status"] == "SENDING"


def test_stale_sending_claim_can_resend(pending_v7):
    fake = FakeSupabase()
    fake.email_row = {"delivery_status": "SENDING"}
    fake.email_sending_fresh = False
    register = RegisterBox()
    emails = []
    _consumed_claim(fake)
    body = _run_complete(fake, register, emails)
    assert len(emails) == 1
    assert body["email_sent"] is True
    assert fake.email_row["delivery_status"] == "SENT"


def test_db_rpc_maps_structured_message_not_sqlstate():
    exc = APIError({"message": "payment_session_expired", "code": "P0001"})
    assert db_rpc_error_identifier(exc) == "payment_session_expired"
    other = APIError({"message": "something else", "code": "P0001"})
    assert db_rpc_error_identifier(other) is None


def test_db_rpc_ignores_identifier_substring_in_str(pending_v7):
    fake = FakeSupabase()
    fake.claim_exc = RuntimeError(
        "{'message': 'payment_session_expired', 'code': 'P0001'}"
    )
    register = RegisterBox()
    with pytest.raises(PaymentCompletionError) as ctx:
        _run_complete(fake, register, [])
    assert db_rpc_error_identifier(fake.claim_exc) is None
    assert ctx.value.status == 500
    assert register.calls == []


def test_db_rpc_code_field_only_when_it_is_the_identifier():
    exc = APIError({"message": "nope", "code": "payment_session_expired"})
    assert db_rpc_error_identifier(exc) == "payment_session_expired"


def test_fresh_second_email_claim_is_in_progress_and_keeps_claim_a(pending_v7):
    fake = FakeSupabase()
    fake.email_row = {"delivery_status": "PENDING", "claim_id": None}
    fake.next_claim_ids = [CLAIM_A, CLAIM_B]
    first = fake._claim_email()
    assert first["should_send"] is True
    assert first["claim_id"] == CLAIM_A
    second = fake._claim_email()
    assert second["in_progress"] is True
    assert second["should_send"] is False
    assert "claim_id" not in second
    assert fake.email_row["claim_id"] == CLAIM_A
    assert fake.email_row["delivery_status"] == "SENDING"


def test_stale_lease_reclaim_mints_new_claim_id(pending_v7):
    fake = FakeSupabase()
    fake.email_row = {"delivery_status": "PENDING", "claim_id": None}
    fake.next_claim_ids = [CLAIM_A, CLAIM_B]
    first = fake._claim_email()
    assert first["claim_id"] == CLAIM_A
    fake.email_sending_fresh = False
    second = fake._claim_email()
    assert second["should_send"] is True
    assert second["claim_id"] == CLAIM_B
    assert second["claim_id"] != first["claim_id"]
    assert fake.email_row["claim_id"] == CLAIM_B


def test_stale_worker_cannot_release_or_mark_newer_claim(pending_v7):
    fake = FakeSupabase()
    fake.email_row = {"delivery_status": "PENDING", "claim_id": None}
    fake.next_claim_ids = [CLAIM_A, CLAIM_B]
    fake._claim_email()
    fake.email_sending_fresh = False
    fake._claim_email()
    assert fake.email_row["claim_id"] == CLAIM_B
    with pytest.raises(APIError) as release_ctx:
        fake._release_email({"p_claim_id": CLAIM_A})
    assert db_rpc_error_identifier(release_ctx.value) == "stale_email_claim"
    assert fake.email_row["delivery_status"] == "SENDING"
    assert fake.email_row["claim_id"] == CLAIM_B
    with pytest.raises(APIError) as mark_ctx:
        fake._mark_email({"p_claim_id": CLAIM_A})
    assert db_rpc_error_identifier(mark_ctx.value) == "stale_email_claim"
    assert fake.email_row["delivery_status"] == "SENDING"
    assert fake.email_row["claim_id"] == CLAIM_B
    fake._mark_email({"p_claim_id": CLAIM_B})
    assert fake.email_row["delivery_status"] == "SENT"
    assert fake.email_row["claim_id"] is None


def test_stale_worker_mark_after_smtp_success_cannot_mark_newer_lease(pending_v7):
    fake = FakeSupabase()
    fake.next_claim_ids = [CLAIM_A, CLAIM_B]
    register = RegisterBox()

    def send_then_reclaim(confirmation):
        fake.email_sending_fresh = False
        reclaimed = fake._claim_email()
        assert reclaimed["claim_id"] == CLAIM_B
        return True, None

    body = complete_pending_payment(
        payment_session_token=RAW_TOKEN,
        data_key=DATA_KEY,
        supabase=fake,
        fetch_confirmation=lambda ref, tok: {"booking_reference": ref},
        send_email=send_then_reclaim,
        register_credential=register,
    )
    assert body["success"] is True
    assert body["email_sent"] is True
    assert CLAIM_A not in json.dumps(body)
    assert CLAIM_B not in json.dumps(body)
    assert "claim_id" not in json.dumps(body)
    assert fake.email_row["delivery_status"] == "SENDING"
    assert fake.email_row["claim_id"] == CLAIM_B


def test_stale_worker_release_after_provider_failure_cannot_clear_newer_lease(
    pending_v7,
):
    fake = FakeSupabase()
    fake.next_claim_ids = [CLAIM_A, CLAIM_B]
    register = RegisterBox()

    def send_fail_after_reclaim(confirmation):
        fake.email_sending_fresh = False
        reclaimed = fake._claim_email()
        assert reclaimed["claim_id"] == CLAIM_B
        return False, "smtp failed"

    body = complete_pending_payment(
        payment_session_token=RAW_TOKEN,
        data_key=DATA_KEY,
        supabase=fake,
        fetch_confirmation=lambda ref, tok: {"booking_reference": ref},
        send_email=send_fail_after_reclaim,
        register_credential=register,
    )
    assert body["success"] is True
    assert body["email_sent"] is False
    assert fake.email_row["delivery_status"] == "SENDING"
    assert fake.email_row["claim_id"] == CLAIM_B


def test_provider_failure_with_current_claim_returns_pending(pending_v7):
    fake = FakeSupabase()
    fake.next_claim_ids = [CLAIM_A]
    register = RegisterBox()

    def send_fail(confirmation):
        assert fake.email_row["claim_id"] == CLAIM_A
        return False, "smtp failed"

    body = complete_pending_payment(
        payment_session_token=RAW_TOKEN,
        data_key=DATA_KEY,
        supabase=fake,
        fetch_confirmation=lambda ref, tok: {"booking_reference": ref},
        send_email=send_fail,
        register_credential=register,
    )
    assert body["email_sent"] is False
    assert fake.email_row["delivery_status"] == "PENDING"
    assert fake.email_row["claim_id"] is None
    release_args = next(
        item[2]
        for item in fake.order
        if item[0] == "rpc"
        and item[1] == "release_reservation_confirmation_email_claim"
    )
    assert release_args["p_claim_id"] == CLAIM_A


def test_sent_cannot_be_reclaimed(pending_v7):
    fake = FakeSupabase()
    fake.email_row = {"delivery_status": "SENT", "claim_id": None}
    result = fake._claim_email()
    assert result["already_sent"] is True
    assert result["should_send"] is False
    assert "claim_id" not in result
    fake.next_claim_ids = [CLAIM_A]
    again = fake._claim_email()
    assert again["already_sent"] is True
    assert fake.email_row["delivery_status"] == "SENT"
    assert fake.email_row["claim_id"] is None


def test_claim_id_never_in_browser_json_or_logs(pending_v7, caplog):
    fake = FakeSupabase()
    fake.next_claim_ids = [CLAIM_A]
    register = RegisterBox()
    emails = []
    with caplog.at_level(logging.DEBUG):
        body = _run_complete(fake, register, emails)
    dumped = json.dumps(body)
    assert CLAIM_A not in dumped
    assert CLAIM_B not in dumped
    assert '"claim_id"' not in dumped
    assert CLAIM_A not in caplog.text
    assert CLAIM_B not in caplog.text
    mark_args = next(
        item[2]
        for item in fake.order
        if item[0] == "rpc"
        and item[1] == "mark_reservation_confirmation_email_sent"
    )
    assert mark_args["p_claim_id"] == CLAIM_A
    assert ATTEMPT_A not in dumped
    assert ATTEMPT_A not in caplog.text


def test_failed_registration_reopens_and_second_card_uses_new_attempt(pending_v7):
    fake = FakeSupabase()
    fake.claim_stateful = True
    fake.session_status = "OPEN"
    fake.current_attempt_key = None
    fake.next_attempt_ids = [ATTEMPT_A, ATTEMPT_B]
    fake.credential_status = "FAILED"
    register = RegisterBox()
    register.exc = CredentialRegistrationError("Card validation was rejected")

    with pytest.raises(PaymentCompletionError) as first:
        _run_complete(fake, register, [])
    assert first.value.status == 422
    assert first.value.retry_payment is True
    assert ATTEMPT_A not in first.value.user_message
    assert fake.session_status == "OPEN"
    assert fake.current_attempt_key is None
    assert register.calls[0]["idempotency_key"] == ATTEMPT_A
    assert register.calls[0]["data_key"] == DATA_KEY
    reopen_args = next(
        item[2]
        for item in fake.order
        if item[0] == "rpc"
        and item[1] == "reopen_payment_session_after_failed_registration"
    )
    assert reopen_args["p_session_id"] == PAYMENT_SESSION
    assert reopen_args["p_registration_idempotency_key"] == ATTEMPT_A

    register.exc = None
    second_key = "Z" * 26
    second = complete_pending_payment(
        payment_session_token=RAW_TOKEN,
        data_key=second_key,
        supabase=fake,
        fetch_confirmation=lambda ref, tok: {"booking_reference": ref},
        send_email=lambda confirmation: (True, None),
        register_credential=register,
    )
    assert second["success"] is True
    assert register.calls[1]["idempotency_key"] == ATTEMPT_B
    assert register.calls[1]["idempotency_key"] != ATTEMPT_A
    assert register.calls[1]["data_key"] == second_key
    assert register.calls[1]["data_key"] != DATA_KEY
    assert ATTEMPT_A not in json.dumps(second)
    assert ATTEMPT_B not in json.dumps(second)
    finalize_args = next(
        item[2]
        for item in fake.order
        if item[0] == "rpc" and item[1] == "finalize_booking_after_credential"
    )
    assert finalize_args == {"p_session_id": PAYMENT_SESSION}


def test_processing_replay_does_not_mint_or_register(pending_v7):
    fake = FakeSupabase()
    fake.claim_stateful = True
    fake.session_status = "PROCESSING"
    fake.current_attempt_key = ATTEMPT_A
    register = RegisterBox()
    with pytest.raises(PaymentCompletionError) as ctx:
        _run_complete(fake, register, [])
    assert ctx.value.status == 409
    assert ctx.value.retry_payment is False
    assert register.calls == []
    assert fake.current_attempt_key == ATTEMPT_A


def test_failed_attempt_a_cannot_reopen_attempt_b(pending_v7):
    fake = FakeSupabase()
    fake.current_attempt_key = ATTEMPT_B
    fake.credential_status = "FAILED"
    register = RegisterBox()
    register.exc = CredentialRegistrationError("Card validation was rejected")
    fake.claim_result["current_registration_idempotency_key"] = ATTEMPT_A
    with pytest.raises(PaymentCompletionError) as ctx:
        _run_complete(fake, register, [])
    assert ctx.value.retry_payment is False
    assert fake.session_status == "PROCESSING"
    assert fake.current_attempt_key == ATTEMPT_B


def test_reconciliation_required_does_not_reopen(pending_v7):
    fake = FakeSupabase()
    fake.credential_status = "RECONCILIATION_REQUIRED"
    register = RegisterBox()
    register.result = SimpleNamespace(registration_status="RECONCILIATION_REQUIRED")
    with pytest.raises(PaymentCompletionError) as ctx:
        _run_complete(fake, register, [])
    assert ctx.value.status == 502
    assert ctx.value.retry_payment is False
    assert not any(
        item[1] == "reopen_payment_session_after_failed_registration"
        for item in fake.order
    )
    assert fake.session_status == "PROCESSING"


def test_pending_credential_cannot_reopen(pending_v7):
    fake = FakeSupabase()
    fake.credential_status = "PENDING"
    register = RegisterBox()
    register.exc = CredentialPersistenceError("persistence")
    with pytest.raises(PaymentCompletionError) as ctx:
        _run_complete(fake, register, [])
    assert ctx.value.retry_payment is False
    assert fake.session_status == "PROCESSING"


def test_succeeded_does_not_reopen(pending_v7):
    fake = FakeSupabase()
    fake.credential_status = "SUCCEEDED"
    register = RegisterBox()
    body = _run_complete(fake, register, [])
    assert body["success"] is True
    assert not any(
        item[1] == "reopen_payment_session_after_failed_registration"
        for item in fake.order
    )


def test_expired_session_cannot_reopen(pending_v7):
    fake = FakeSupabase()
    fake.credential_status = "FAILED"
    fake.expires_future = False
    register = RegisterBox()
    register.exc = CredentialRegistrationError("Card validation was rejected")
    with pytest.raises(PaymentCompletionError) as ctx:
        _run_complete(fake, register, [])
    assert ctx.value.retry_payment is False
    assert fake.session_status == "PROCESSING"


def test_mixed_reservation_cannot_reopen(pending_v7):
    fake = FakeSupabase()
    fake.credential_status = "FAILED"
    fake.reservation_uniform_pending = False
    register = RegisterBox()
    register.exc = CredentialRegistrationError("Card validation was rejected")
    with pytest.raises(PaymentCompletionError) as ctx:
        _run_complete(fake, register, [])
    assert ctx.value.retry_payment is False
    assert fake.session_status == "PROCESSING"


def test_http_retry_payment_only_after_reopen(
    client, unpaused, pending_v7, completion_stack
):
    fake, register, _emails = completion_stack
    fake.credential_status = "FAILED"
    register.exc = CredentialRegistrationError("Card validation was rejected")
    resp = client.post("/api/complete-payment", json=_valid_body())
    assert resp.status_code == 422
    body = resp.get_json()
    assert body["success"] is False
    assert body["retry_payment"] is True
    assert ATTEMPT_A not in json.dumps(body)
    assert "current_registration_idempotency_key" not in json.dumps(body)
    assert leaked_internal_keys(body) == set()


def test_http_422_without_reopen_has_no_retry_flag(
    client, unpaused, pending_v7, completion_stack
):
    fake, register, _emails = completion_stack
    fake.credential_status = "PENDING"
    register.exc = CredentialRegistrationError("Card validation was rejected")
    resp = client.post("/api/complete-payment", json=_valid_body())
    assert resp.status_code == 422
    body = resp.get_json()
    assert body["success"] is False
    assert "retry_payment" not in body


def test_parse_error_before_claim_is_explicitly_retryable():
    with pytest.raises(PaymentCompletionError) as ctx:
        parse_browser_payment_request({"payment_session_token": RAW_TOKEN})
    assert ctx.value.status == 400
    assert ctx.value.retry_payment is True

