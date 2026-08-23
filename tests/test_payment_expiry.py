"""Caller endpoint for expire_abandoned_payment_sessions().

Does not execute sql/003 or call Moneris. Supabase RPC is mocked.
"""

from __future__ import annotations

import inspect
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

import main
import payment_expiry
from payment_expiry import (
    CRON_SECRET_ENV,
    EXPIRE_RPC_NAME,
    MIN_CRON_SECRET_LENGTH,
    RECOMMENDED_CADENCE_MINUTES,
    SAFE_COUNTER_KEYS,
    authorize_expiry_cron,
    validate_expire_rpc_result,
)


WEBSITE_ROOT = Path(__file__).resolve().parents[1]
CRON_SECRET = "c" * MIN_CRON_SECRET_LENGTH
OTHER_SECRET = "d" * MIN_CRON_SECRET_LENGTH
ASSERT_SECRET_NOT_IN = (CRON_SECRET, OTHER_SECRET)

UNSAFE_RPC_FIELDS = {
    "session_id": "s-1",
    "reservation_id": "r-1",
    "canonical_booking_id": "b-1",
    "booking_reference": "BK-LEAK",
    "current_registration_idempotency_key": "attempt-a",
    "credential_id": "cred-1",
    "moneris_payment_method_id": "pmid-1",
    "paymentMethodId": "pmid-1",
    "guest_email": "guest@example.com",
}

SAFE_RPC = {
    "expired": 1,
    "inconsistent": 2,
    "held_pending": 3,
    "held_reconciliation": 4,
}


class FakeRpc:
    def __init__(self, owner, name, args):
        self.owner = owner
        self.name = name
        self.args = args

    def execute(self):
        self.owner.executed.append((self.name, dict(self.args)))
        if self.owner.exc:
            raise self.owner.exc
        return SimpleNamespace(data=dict(self.owner.payload))


class FakeSupabase:
    def __init__(self, payload=None, exc=None):
        self.payload = payload if payload is not None else dict(SAFE_RPC)
        self.exc = exc
        self.executed = []

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
def cron_secret(monkeypatch):
    monkeypatch.setenv(CRON_SECRET_ENV, CRON_SECRET)


@pytest.fixture
def expiry_stack(monkeypatch, pending_v7, cron_secret):
    fake = FakeSupabase()
    monkeypatch.setattr(main, "supabase", fake)
    return fake


def _auth(**extra_headers):
    headers = {"Authorization": f"Bearer {CRON_SECRET}"}
    headers.update(extra_headers)
    return headers


def _forbid_network(monkeypatch):
    def fail(*_args, **_kwargs):
        raise AssertionError("network request is forbidden in expiry tests")

    monkeypatch.setattr(requests, "request", fail)
    monkeypatch.setattr(requests, "post", fail)
    monkeypatch.setattr(requests, "get", fail)
    monkeypatch.setattr(requests, "put", fail)
    monkeypatch.setattr(requests, "patch", fail)
    monkeypatch.setattr(requests, "delete", fail)


def test_direct_bookings_remain_paused():
    assert main.DIRECT_BOOKINGS_PAUSED is True


def test_missing_cron_secret_env_fails_closed(client, pending_v7, monkeypatch):
    monkeypatch.delenv(CRON_SECRET_ENV, raising=False)
    fake = FakeSupabase()
    monkeypatch.setattr(main, "supabase", fake)
    resp = client.post(
        "/api/internal/expire-payment-sessions",
        headers={"Authorization": f"Bearer {CRON_SECRET}"},
    )
    assert resp.status_code == 503
    assert resp.get_json()["ok"] is False
    assert fake.executed == []


def test_blank_and_short_cron_secret_fail_closed(client, pending_v7, monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setattr(main, "supabase", fake)
    for value in ("", "   ", "short"):
        monkeypatch.setenv(CRON_SECRET_ENV, value)
        resp = client.post(
            "/api/internal/expire-payment-sessions",
            headers={"Authorization": f"Bearer {value.strip() or 'x'}"},
        )
        assert resp.status_code == 503, value
        assert fake.executed == []


def test_missing_bearer_header_rejected(client, expiry_stack):
    resp = client.post("/api/internal/expire-payment-sessions")
    assert resp.status_code == 401
    assert resp.get_json()["ok"] is False
    assert expiry_stack.executed == []


def test_wrong_scheme_rejected(client, expiry_stack):
    resp = client.post(
        "/api/internal/expire-payment-sessions",
        headers={"Authorization": f"Basic {CRON_SECRET}"},
    )
    assert resp.status_code == 401
    assert expiry_stack.executed == []


def test_wrong_bearer_secret_rejected(client, expiry_stack):
    resp = client.post(
        "/api/internal/expire-payment-sessions",
        headers={"Authorization": f"Bearer {OTHER_SECRET}"},
    )
    assert resp.status_code == 401
    assert expiry_stack.executed == []


def test_query_string_cannot_authenticate(client, expiry_stack):
    resp = client.post(
        f"/api/internal/expire-payment-sessions?secret={CRON_SECRET}&token={CRON_SECRET}"
    )
    assert resp.status_code == 401
    assert expiry_stack.executed == []


def test_correct_secret_invokes_rpc(client, expiry_stack):
    resp = client.post("/api/internal/expire-payment-sessions", headers=_auth())
    assert resp.status_code == 200
    assert expiry_stack.executed == [(EXPIRE_RPC_NAME, {})]
    body = resp.get_json()
    assert body == {"ok": True, **SAFE_RPC}


def test_secret_comparison_uses_compare_digest_not_plain_eq(cron_secret):
    src = inspect.getsource(payment_expiry._secrets_match)
    assert "secrets.compare_digest" in src
    assert "provided == expected" not in src
    assert "expected == provided" not in src
    authorize_expiry_cron(f"Bearer {CRON_SECRET}")
    with pytest.raises(payment_expiry.PaymentExpiryError) as exc:
        authorize_expiry_cron(f"Bearer {OTHER_SECRET}")
    assert exc.value.status == 401


def test_live_v6_does_not_invoke_rpc(client, cron_secret, monkeypatch):
    monkeypatch.setenv("CREATE_PUBLIC_BOOKING_CONTRACT", "live_v6")
    fake = FakeSupabase()
    monkeypatch.setattr(main, "supabase", fake)
    resp = client.post("/api/internal/expire-payment-sessions", headers=_auth())
    assert resp.status_code == 503
    assert fake.executed == []


def test_invalid_booking_contract_does_not_invoke_rpc(client, cron_secret, monkeypatch):
    monkeypatch.setenv("CREATE_PUBLIC_BOOKING_CONTRACT", "pending")
    fake = FakeSupabase()
    monkeypatch.setattr(main, "supabase", fake)
    resp = client.post("/api/internal/expire-payment-sessions", headers=_auth())
    assert resp.status_code == 503
    assert fake.executed == []


def test_pending_v7_may_execute(client, expiry_stack):
    assert payment_expiry.booking_rpc_contract() == "pending_v7"
    resp = client.post("/api/internal/expire-payment-sessions", headers=_auth())
    assert resp.status_code == 200
    assert expiry_stack.executed[0][0] == EXPIRE_RPC_NAME


def test_works_while_direct_bookings_paused(client, expiry_stack):
    assert main.DIRECT_BOOKINGS_PAUSED is True
    resp = client.post("/api/internal/expire-payment-sessions", headers=_auth())
    assert resp.status_code == 200
    assert expiry_stack.executed


def test_no_request_body_is_needed(client, expiry_stack):
    resp = client.post(
        "/api/internal/expire-payment-sessions",
        headers=_auth(),
        data=b"",
    )
    assert resp.status_code == 200
    assert expiry_stack.executed == [(EXPIRE_RPC_NAME, {})]


def test_client_cannot_supply_booking_or_session_ids(client, expiry_stack):
    poisoned = {key: f"client-{key}" for key in payment_expiry.REJECTED_CLIENT_ID_KEYS}
    resp = client.post(
        "/api/internal/expire-payment-sessions",
        headers=_auth(),
        json=poisoned,
    )
    assert resp.status_code == 200
    assert expiry_stack.executed == [(EXPIRE_RPC_NAME, {})]
    body = resp.get_json()
    for key in payment_expiry.REJECTED_CLIENT_ID_KEYS:
        assert key not in body


def test_safe_rpc_counters_only(client, expiry_stack):
    resp = client.post("/api/internal/expire-payment-sessions", headers=_auth())
    body = resp.get_json()
    assert set(body) == {"ok"} | set(SAFE_COUNTER_KEYS)
    for key in SAFE_COUNTER_KEYS:
        assert type(body[key]) is int


def test_unexpected_rpc_fields_are_dropped(client, monkeypatch, pending_v7, cron_secret):
    fake = FakeSupabase(payload={**SAFE_RPC, **UNSAFE_RPC_FIELDS})
    monkeypatch.setattr(main, "supabase", fake)
    resp = client.post("/api/internal/expire-payment-sessions", headers=_auth())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body == {"ok": True, **SAFE_RPC}
    for key in UNSAFE_RPC_FIELDS:
        assert key not in body
    dumped = json.dumps(body)
    for value in UNSAFE_RPC_FIELDS.values():
        assert value not in dumped


def test_invalid_rpc_shape_is_safe_failure(client, monkeypatch, pending_v7, cron_secret):
    fake = FakeSupabase(payload={"expired": "1", "inconsistent": 0})
    monkeypatch.setattr(main, "supabase", fake)
    resp = client.post("/api/internal/expire-payment-sessions", headers=_auth())
    assert resp.status_code == 500
    assert resp.get_json() == {
        "ok": False,
        "error": payment_expiry.SAFE_FAILED,
    }


def test_supabase_exception_is_generic_safe_failure(
    client, monkeypatch, pending_v7, cron_secret, caplog
):
    class Boom(Exception):
        pass

    fake = FakeSupabase(exc=Boom("session_id=s-1 attempt=a pmid=x"))
    monkeypatch.setattr(main, "supabase", fake)
    with caplog.at_level(logging.ERROR):
        resp = client.post("/api/internal/expire-payment-sessions", headers=_auth())
    assert resp.status_code == 500
    assert resp.get_json()["error"] == payment_expiry.SAFE_FAILED
    assert "session_id=s-1" not in resp.get_data(as_text=True)
    assert "Boom" in caplog.text
    assert "session_id=s-1" not in caplog.text
    assert "pmid=x" not in caplog.text


def test_logs_do_not_contain_cron_secret(
    client, expiry_stack, caplog, cron_secret
):
    with caplog.at_level(logging.DEBUG):
        client.post(
            "/api/internal/expire-payment-sessions",
            headers={"Authorization": f"Bearer {OTHER_SECRET}"},
        )
        client.post("/api/internal/expire-payment-sessions", headers=_auth())
    text = caplog.text
    for secret in ASSERT_SECRET_NOT_IN:
        assert secret not in text


def test_repeated_calls_are_permitted(client, expiry_stack):
    first = client.post("/api/internal/expire-payment-sessions", headers=_auth())
    second = client.post("/api/internal/expire-payment-sessions", headers=_auth())
    assert first.status_code == 200
    assert second.status_code == 200
    assert expiry_stack.executed == [
        (EXPIRE_RPC_NAME, {}),
        (EXPIRE_RPC_NAME, {}),
    ]


def test_two_callers_do_not_need_application_mutex():
    src = Path(payment_expiry.__file__).read_text(encoding="utf-8")
    route = inspect.getsource(main.handle_expire_payment_sessions)
    combined = src + "\n" + route
    for needle in (
        "Lock(",
        "RLock(",
        "redis",
        "fcntl",
        "filelock",
        "APScheduler",
        "BackgroundScheduler",
        "threading.Timer",
        "while True",
    ):
        assert needle not in combined
    assert "SKIP LOCKED" in src or "SKIP LOCKED" in payment_expiry.__doc__


def test_no_in_process_scheduler_or_pg_cron():
    main_src = Path(main.__file__).read_text(encoding="utf-8")
    expiry_src = Path(payment_expiry.__file__).read_text(encoding="utf-8")
    vercel = json.loads((WEBSITE_ROOT / "vercel.json").read_text(encoding="utf-8"))
    combined = main_src + "\n" + expiry_src
    assert "APScheduler" not in combined
    assert "BackgroundScheduler" not in combined
    assert "threading.Timer" not in combined
    assert "schedule.every" not in combined
    assert "pg_cron" not in combined
    assert "CREATE EXTENSION" not in combined
    assert "crons" not in vercel
    assert RECOMMENDED_CADENCE_MINUTES == 5


def test_no_network_except_mocked_supabase(client, expiry_stack, monkeypatch):
    _forbid_network(monkeypatch)
    resp = client.post("/api/internal/expire-payment-sessions", headers=_auth())
    assert resp.status_code == 200
    assert expiry_stack.executed == [(EXPIRE_RPC_NAME, {})]


def test_validate_expire_rpc_result_rejects_bool_and_negative():
    with pytest.raises(payment_expiry.PaymentExpiryError):
        validate_expire_rpc_result({**SAFE_RPC, "expired": True})
    with pytest.raises(payment_expiry.PaymentExpiryError):
        validate_expire_rpc_result({**SAFE_RPC, "expired": -1})


def test_sql_return_contract_matches_safe_counters():
    sql = (
        Path(__file__).resolve().parents[2]
        / "grande_mountain_lodge_payments_api"
        / "sql"
        / "003_pending_payment_sessions.sql"
    ).read_text(encoding="utf-8")
    expire = sql.split(
        "CREATE OR REPLACE FUNCTION public.expire_abandoned_payment_sessions()",
        1,
    )[1]
    ret = expire.split("RETURN jsonb_build_object(", 1)[1]
    ret = ret.split(");", 1)[0]
    for key in SAFE_COUNTER_KEYS:
        assert f"'{key}'" in ret
    assert "session_id" not in ret
    assert "DO NOT execute from this repository in this stage" in sql
