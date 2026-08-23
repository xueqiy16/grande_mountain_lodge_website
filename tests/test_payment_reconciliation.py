"""Operator recovery path for held payment registrations.

Does not execute sql/003 or call Moneris. Supabase RPC is mocked.
"""

from __future__ import annotations

import inspect
import json
import logging
from types import SimpleNamespace
from uuid import uuid4

import pytest
import requests

import main
import payment_expiry
import payment_reconciliation
from payment_reconciliation import (
    ADMIN_SECRET_ENV,
    CRON_SECRET_ENV,
    FINALIZE_RPC_NAME,
    HELD_ITEM_KEYS,
    LIST_RPC_NAME,
    MIN_ADMIN_SECRET_LENGTH,
    RELEASE_RPC_NAME,
    UNSAFE_OPERATOR_FIELDS,
    authorize_reconciliation_admin,
)


ADMIN_SECRET = "a" * MIN_ADMIN_SECRET_LENGTH
CRON_SECRET = "c" * MIN_ADMIN_SECRET_LENGTH
OTHER_SECRET = "d" * MIN_ADMIN_SECRET_LENGTH
SESSION_ID = str(uuid4())
ATTEMPT_A = str(uuid4())
ATTEMPT_B = str(uuid4())

HELD_ITEM = {
    "session_id": SESSION_ID,
    "reservation_id": str(uuid4()),
    "booking_reference": "BK-HELD",
    "session_status": "PROCESSING",
    "session_created_at": "2026-08-22T12:00:00+00:00",
    "processing_started_at": "2026-08-22T12:01:00+00:00",
    "expires_at": "2026-08-22T12:20:00+00:00",
    "held_seconds": 900,
    "current_registration_idempotency_key": ATTEMPT_A,
    "credential_id": str(uuid4()),
    "registration_status": "PENDING",
    "registration_error_category": None,
    "credential_created_at": "2026-08-22T12:01:05+00:00",
    "credential_updated_at": "2026-08-22T12:01:05+00:00",
    "moneris_payment_method_id": "MUST-DROP",
    "dataKey": "tok_must_drop",
    "guest_email": "guest@example.com",
}


class FakeRpc:
    def __init__(self, owner, name, args):
        self.owner = owner
        self.name = name
        self.args = args

    def execute(self):
        self.owner.executed.append((self.name, dict(self.args)))
        if self.owner.exc_for.get(self.name):
            raise self.owner.exc_for[self.name]
        return SimpleNamespace(data=self.owner.payloads.get(self.name, {}))


class FakeSupabase:
    def __init__(self):
        self.executed = []
        self.payloads = {
            LIST_RPC_NAME: {"ok": True, "held": [dict(HELD_ITEM)]},
            FINALIZE_RPC_NAME: {
                "ok": True,
                "idempotent": False,
                "booking_reference": "BK-HELD",
            },
            RELEASE_RPC_NAME: {"ok": True, "booking_reference": "BK-HELD"},
        }
        self.exc_for = {}

    def rpc(self, name, args=None):
        return FakeRpc(self, name, args or {})


@pytest.fixture
def client():
    main.app.config.update(TESTING=True)
    main.limiter.enabled = False
    try:
        yield main.app.test_client()
    finally:
        main.limiter.enabled = True


@pytest.fixture
def pending_v7(monkeypatch):
    monkeypatch.setenv("CREATE_PUBLIC_BOOKING_CONTRACT", "pending_v7")


@pytest.fixture
def admin_secret(monkeypatch):
    monkeypatch.setenv(ADMIN_SECRET_ENV, ADMIN_SECRET)
    monkeypatch.setenv(CRON_SECRET_ENV, CRON_SECRET)


@pytest.fixture
def stack(monkeypatch, pending_v7, admin_secret):
    fake = FakeSupabase()
    monkeypatch.setattr(main, "supabase", fake)
    return fake


def _auth(secret=ADMIN_SECRET, **extra):
    headers = {"Authorization": f"Bearer {secret}"}
    headers.update(extra)
    return headers


def _forbid_network(monkeypatch):
    def fail(*_a, **_k):
        raise AssertionError("network request is forbidden in reconciliation tests")

    for name in ("request", "post", "get", "put", "patch", "delete"):
        monkeypatch.setattr(requests, name, fail)


def test_direct_bookings_remain_paused():
    assert main.DIRECT_BOOKINGS_PAUSED is True


def test_missing_admin_secret_fails_closed(client, pending_v7, monkeypatch):
    monkeypatch.delenv(ADMIN_SECRET_ENV, raising=False)
    fake = FakeSupabase()
    monkeypatch.setattr(main, "supabase", fake)
    resp = client.get(
        "/api/internal/payment-reconciliation/held",
        headers=_auth(),
    )
    assert resp.status_code == 503
    assert fake.executed == []


def test_wrong_bearer_rejected(client, stack):
    resp = client.get(
        "/api/internal/payment-reconciliation/held",
        headers=_auth(OTHER_SECRET),
    )
    assert resp.status_code == 401
    assert stack.executed == []


def test_expiry_cron_secret_cannot_authorize(client, stack):
    resp = client.get(
        "/api/internal/payment-reconciliation/held",
        headers=_auth(CRON_SECRET),
    )
    assert resp.status_code == 401
    assert stack.executed == []
    assert CRON_SECRET != ADMIN_SECRET


def test_query_string_auth_rejected(client, stack):
    resp = client.get(
        f"/api/internal/payment-reconciliation/held?secret={ADMIN_SECRET}"
    )
    assert resp.status_code == 401
    assert stack.executed == []


def test_compare_digest_not_plain_eq(admin_secret):
    src = inspect.getsource(payment_reconciliation._secrets_match)
    assert "secrets.compare_digest" in src
    assert "provided == expected" not in src
    authorize_reconciliation_admin(f"Bearer {ADMIN_SECRET}")
    with pytest.raises(payment_reconciliation.PaymentReconciliationError) as exc:
        authorize_reconciliation_admin(f"Bearer {CRON_SECRET}")
    assert exc.value.status == 401


def test_list_returns_current_attempt_safe_fields_only(client, stack):
    resp = client.get(
        "/api/internal/payment-reconciliation/held", headers=_auth()
    )
    assert resp.status_code == 200
    assert stack.executed == [(LIST_RPC_NAME, {})]
    body = resp.get_json()
    assert body["ok"] is True
    assert len(body["held"]) == 1
    item = body["held"][0]
    assert item["registration_status"] == "PENDING"
    assert item["current_registration_idempotency_key"] == ATTEMPT_A
    assert item["held_seconds"] == 900
    assert set(item) <= set(HELD_ITEM_KEYS)
    dumped = json.dumps(body)
    for field in UNSAFE_OPERATOR_FIELDS:
        assert field not in item
        assert "MUST-DROP" not in dumped
        assert "tok_must_drop" not in dumped
        assert "guest@example.com" not in dumped
    assert "reservation_id" not in item


def test_old_failed_attempt_is_not_in_list_contract():
    src = inspect.getsource(payment_reconciliation.list_held_payment_registrations)
    assert LIST_RPC_NAME in src
    sql_helper = (
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "grande_mountain_lodge_payments_api"
        / "sql"
        / "003_pending_payment_sessions.sql"
    ).read_text(encoding="utf-8")
    listed = sql_helper.split(
        "CREATE OR REPLACE FUNCTION public.list_held_payment_registrations()", 1
    )[1]
    listed = listed.split("operator_finalize_held_payment", 1)[0]
    assert "PENDING" in listed
    assert "RECONCILIATION_REQUIRED" in listed
    assert "'FAILED'" not in listed


def test_age_does_not_auto_resolve(client, stack):
    resp = client.get(
        "/api/internal/payment-reconciliation/held", headers=_auth()
    )
    assert resp.status_code == 200
    assert resp.get_json()["held"][0]["held_seconds"] == 900
    assert stack.executed == [(LIST_RPC_NAME, {})]


def test_works_while_paused(client, stack):
    assert main.DIRECT_BOOKINGS_PAUSED is True
    resp = client.get(
        "/api/internal/payment-reconciliation/held", headers=_auth()
    )
    assert resp.status_code == 200


def test_live_v6_does_not_invoke_rpc(client, admin_secret, monkeypatch):
    monkeypatch.setenv("CREATE_PUBLIC_BOOKING_CONTRACT", "live_v6")
    fake = FakeSupabase()
    monkeypatch.setattr(main, "supabase", fake)
    resp = client.get(
        "/api/internal/payment-reconciliation/held", headers=_auth()
    )
    assert resp.status_code == 503
    assert fake.executed == []


def test_finalize_succeeded_invokes_operator_rpc(client, stack):
    resp = client.post(
        f"/api/internal/payment-reconciliation/{SESSION_ID}/finalize",
        headers=_auth(),
        json={},
    )
    assert resp.status_code == 200
    assert stack.executed == [
        (FINALIZE_RPC_NAME, {"p_session_id": SESSION_ID})
    ]
    body = resp.get_json()
    assert body["ok"] is True
    assert body["idempotent"] is False
    assert "reservation_id" not in body


def test_finalize_idempotent(client, stack):
    stack.payloads[FINALIZE_RPC_NAME] = {
        "ok": True,
        "idempotent": True,
        "booking_reference": "BK-HELD",
    }
    resp = client.post(
        f"/api/internal/payment-reconciliation/{SESSION_ID}/finalize",
        headers=_auth(),
    )
    assert resp.status_code == 200
    assert resp.get_json()["idempotent"] is True


def test_finalize_rejects_operator_supplied_moneris_ids(client, stack):
    resp = client.post(
        f"/api/internal/payment-reconciliation/{SESSION_ID}/finalize",
        headers=_auth(),
        json={"paymentMethodId": "pmid", "issuerId": "iss"},
    )
    assert resp.status_code == 400
    assert stack.executed == []


@pytest.mark.parametrize(
    "identifier",
    (
        "credential_not_succeeded",
        "payment_session_already_finalized",
        "reservation_not_pending_payment",
    ),
)
def test_finalize_conflict_identifiers(client, stack, identifier):
    class ApiError(Exception):
        def __init__(self, message):
            self.message = message

    stack.exc_for[FINALIZE_RPC_NAME] = ApiError(identifier)
    resp = client.post(
        f"/api/internal/payment-reconciliation/{SESSION_ID}/finalize",
        headers=_auth(),
    )
    assert resp.status_code == 409
    assert resp.get_json()["ok"] is False


def test_release_pending_requires_matching_attempt(client, stack):
    resp = client.post(
        f"/api/internal/payment-reconciliation/{SESSION_ID}/release-confirmed-failure",
        headers=_auth(),
        json={"current_registration_idempotency_key": ATTEMPT_A},
    )
    assert resp.status_code == 200
    name, args = stack.executed[0]
    assert name == RELEASE_RPC_NAME
    assert args["p_session_id"] == SESSION_ID
    assert args["p_current_registration_idempotency_key"] == ATTEMPT_A
    assert "p_operator_source" not in args
    assert ADMIN_SECRET not in json.dumps(args)


def test_release_rejects_missing_attempt_key(client, stack):
    resp = client.post(
        f"/api/internal/payment-reconciliation/{SESSION_ID}/release-confirmed-failure",
        headers=_auth(),
        json={},
    )
    assert resp.status_code == 400
    assert stack.executed == []


class _ApiError(Exception):
    def __init__(self, message):
        self.message = message


@pytest.mark.parametrize(
    ("identifier", "status"),
    (
        ("credential_succeeded_not_releasable", 409),
        ("stale_registration_attempt", 409),
        ("registration_not_held", 409),
        ("payment_session_already_finalized", 409),
        ("reservation_not_pending_payment", 409),
    ),
)
def test_release_conflict_identifiers(client, stack, identifier, status):
    stack.exc_for[RELEASE_RPC_NAME] = _ApiError(identifier)
    resp = client.post(
        f"/api/internal/payment-reconciliation/{SESSION_ID}/release-confirmed-failure",
        headers=_auth(),
        json={"current_registration_idempotency_key": ATTEMPT_A},
    )
    assert resp.status_code == status
    assert resp.get_json()["ok"] is False


def test_release_rejects_moneris_overrides(client, stack):
    resp = client.post(
        f"/api/internal/payment-reconciliation/{SESSION_ID}/release-confirmed-failure",
        headers=_auth(),
        json={
            "current_registration_idempotency_key": ATTEMPT_A,
            "registration_status": "FAILED",
            "paymentMethodId": "x",
        },
    )
    assert resp.status_code == 400
    assert stack.executed == []


def test_logs_omit_admin_secret_and_ids(client, stack, caplog):
    with caplog.at_level(logging.DEBUG):
        client.get(
            "/api/internal/payment-reconciliation/held",
            headers=_auth(OTHER_SECRET),
        )
        client.get(
            "/api/internal/payment-reconciliation/held",
            headers=_auth(),
        )
    text = caplog.text
    assert ADMIN_SECRET not in text
    assert CRON_SECRET not in text
    assert OTHER_SECRET not in text


def test_supabase_exception_is_generic(client, stack, caplog):
    class Boom(Exception):
        pass

    stack.exc_for[LIST_RPC_NAME] = Boom("dataKey=tok session_id=s")
    with caplog.at_level(logging.ERROR):
        resp = client.get(
            "/api/internal/payment-reconciliation/held", headers=_auth()
        )
    assert resp.status_code == 500
    assert resp.get_json()["error"] == payment_reconciliation.SAFE_FAILED
    assert "dataKey=tok" not in caplog.text
    assert "Boom" in caplog.text


def test_no_in_process_moneris_retry_or_age_release():
    src = inspect.getsource(payment_reconciliation) + inspect.getsource(main)
    assert "validate_card" not in src
    assert "register_booking_payment_credential" not in src
    assert "APScheduler" not in src
    assert "held_seconds >" not in src
    assert "max_age" not in src


def test_no_network_except_mocked_supabase(client, stack, monkeypatch):
    _forbid_network(monkeypatch)
    resp = client.get(
        "/api/internal/payment-reconciliation/held", headers=_auth()
    )
    assert resp.status_code == 200
    assert stack.executed == [(LIST_RPC_NAME, {})]


def test_secrets_are_distinct():
    assert ADMIN_SECRET_ENV != payment_expiry.CRON_SECRET_ENV
    assert ADMIN_SECRET_ENV != CRON_SECRET_ENV
