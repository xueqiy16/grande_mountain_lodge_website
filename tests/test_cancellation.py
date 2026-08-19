"""Guest self-service cancellation: token hashing, GET preview, POST cancel."""

from datetime import datetime, timedelta, timezone

import pytest

import main


@pytest.fixture
def client():
    main.app.config.update(TESTING=True)
    main.limiter.enabled = False
    try:
        yield main.app.test_client()
    finally:
        main.limiter.enabled = True


class Result:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    def __init__(self, store, table):
        self.store = store
        self.table = table
        self._filters = []
        self._op = "select"
        self._payload = None
        self._limit = None

    def select(self, *_a, **_k):
        self._op = "select"
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = payload
        return self

    def eq(self, key, value):
        self._filters.append(("eq", key, value))
        return self

    def limit(self, n):
        self._limit = n
        return self

    def _matches(self, row):
        for kind, key, value in self._filters:
            if kind != "eq":
                continue
            left = row.get(key)
            if key == "booking_reference":
                if str(left or "").upper() != str(value or "").upper():
                    return False
            elif left != value:
                return False
        return True

    def execute(self):
        rows = [r for r in self.store[self.table] if self._matches(r)]
        if self._op == "update":
            self.store.setdefault("updates", []).append(
                (self.table, dict(self._payload or {}), list(self._filters))
            )
            for row in rows:
                row.update(self._payload or {})
        if self._limit is not None:
            rows = rows[: self._limit]
        return Result(rows)


class FakeRpc:
    def __init__(self, store, name, params):
        self.store = store
        self.name = name
        self.params = params

    def execute(self):
        self.store.setdefault("rpc_calls", []).append((self.name, dict(self.params)))
        if self.name != "cancel_public_booking":
            raise RuntimeError(f"unexpected rpc {self.name}")
        return Result(_apply_cancel(self.store, self.params))


class FakeSupabase:
    def __init__(self, store):
        self.store = store

    def table(self, name):
        return FakeQuery(self.store, name)

    def rpc(self, name, params):
        return FakeRpc(self.store, name, params)


def _apply_cancel(store, params):
    """In-memory mirror of cancel_public_booking contract for Flask tests."""
    ref = (params.get("p_booking_reference") or "").strip().upper()
    token_hash = params.get("p_cancellation_token_hash")
    if not token_hash or len(token_hash) != 64:
        raise RuntimeError("invalid_cancellation")

    cancel_row = None
    for crow in store["cancellation"]:
        if crow.get("cancellation_token_hash") != token_hash:
            continue
        booking = next(
            (b for b in store["bookings"] if b.get("booking_id") == crow.get("booking_id")),
            None,
        )
        if booking and str(booking.get("booking_reference", "")).upper() == ref:
            cancel_row = crow
            break
    if not cancel_row:
        raise RuntimeError("invalid_cancellation")
    if cancel_row.get("token_usage") is True:
        raise RuntimeError("already_used")
    expiry = main._parse_timestamptz(cancel_row.get("token_expiry"))
    if expiry is None:
        raise RuntimeError("expired")
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if expiry <= datetime.now(timezone.utc):
        raise RuntimeError("expired")

    related = [
        b for b in store["bookings"]
        if str(b.get("booking_reference", "")).upper() == ref
    ]
    if any(b.get("booking_status") == "checked_in" for b in related):
        raise RuntimeError("cannot_cancel")

    now = datetime.now(timezone.utc).isoformat()
    cancelled_count = 0
    for booking in related:
        if booking.get("booking_status") not in ("cancelled", "no_show", "checked_out"):
            booking["booking_status"] = "cancelled"
            booking["cancelled_at"] = booking.get("cancelled_at") or now
            cancelled_count += 1

    related_ids = {b.get("booking_id") for b in related}
    for crow in store["cancellation"]:
        if crow is cancel_row or crow.get("booking_id") in related_ids:
            crow["token_usage"] = True
            crow["token_used_at"] = crow.get("token_used_at") or now

    return {
        "ok": True,
        "booking_reference": ref,
        "cancelled_count": cancelled_count,
        "booking_status": "cancelled",
    }


def _seed_reservation(store, *, ref="BK-TEST1", cancel_token=None, confirmation_token="conf-token",
                      status="confirmed", extra_rooms=0, expiry=None, token_used=False,
                      booking_id="b1"):
    cancel_token = cancel_token or "cancel-token-one"
    token_hash = main._hash_cancellation_token(cancel_token)
    if expiry is None:
        expiry = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    bookings = [{
        "booking_id": booking_id,
        "booking_reference": ref,
        "check_in": "2026-09-01",
        "check_out": "2026-09-03",
        "booking_status": status,
        "cancelled_at": None,
        "total_nights": 2,
        "confirmation_token": confirmation_token,
        "rooms": {"room_types": {"name": "Studio Queen Non-Smoking"}},
    }]
    for i in range(extra_rooms):
        bookings.append({
            "booking_id": f"b{i+2}",
            "booking_reference": ref,
            "check_in": "2026-09-01",
            "check_out": "2026-09-03",
            "booking_status": status,
            "cancelled_at": None,
            "total_nights": 2,
            "confirmation_token": confirmation_token,
            "rooms": {"room_types": {"name": "Studio Double Queen Non-Smoking"}},
        })
    store["bookings"] = bookings
    store["cancellation"] = [{
        "id": "c1",
        "booking_id": booking_id,
        "cancellation_token_hash": token_hash,
        "token_expiry": expiry,
        "token_usage": token_used,
        "token_used_at": None,
    }]
    store["rpc_calls"] = []
    store["updates"] = []
    return cancel_token, confirmation_token


@pytest.fixture
def store(monkeypatch):
    data = {"bookings": [], "cancellation": [], "rpc_calls": [], "updates": []}
    monkeypatch.setattr(main, "supabase", FakeSupabase(data))
    return data


def test_hash_cancellation_token_is_sha256_hex():
    token = "abc"
    digest = main._hash_cancellation_token(token)
    assert len(digest) == 64
    assert digest == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert main._hash_cancellation_token("") == ""
    assert main._hash_cancellation_token(None) == ""


def test_valid_cancel_token_displays_confirmation_page(client, store):
    token, _ = _seed_reservation(store)
    resp = client.get(f"/cancel-reservation/BK-TEST1?token={token}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "BK-TEST1" in body
    assert "Confirm cancellation" in body
    assert "Studio Queen Non-Smoking" in body
    assert store["rpc_calls"] == []
    assert store["bookings"][0]["booking_status"] == "confirmed"


def test_get_does_not_cancel(client, store):
    token, _ = _seed_reservation(store)
    client.get(f"/cancel-reservation/BK-TEST1?token={token}")
    client.get(f"/cancel-reservation/BK-TEST1?token={token}")
    assert store["rpc_calls"] == []
    assert all(b["booking_status"] == "confirmed" for b in store["bookings"])
    assert store["cancellation"][0]["token_usage"] is False


def test_valid_post_cancels_booking_and_sets_cancelled_at(client, store):
    token, _ = _seed_reservation(store)
    resp = client.post(
        "/cancel-reservation/BK-TEST1",
        data={"token": token},
    )
    assert resp.status_code == 200
    assert "Reservation Cancelled" in resp.get_data(as_text=True)
    row = store["bookings"][0]
    assert row["booking_status"] == "cancelled"
    assert row["cancelled_at"]
    assert store["cancellation"][0]["token_usage"] is True
    assert store["cancellation"][0]["token_used_at"]
    assert store["rpc_calls"][0][0] == "cancel_public_booking"


def test_token_becomes_used_and_replay_fails(client, store):
    token, _ = _seed_reservation(store)
    assert client.post("/cancel-reservation/BK-TEST1", data={"token": token}).status_code == 200
    replay = client.post("/cancel-reservation/BK-TEST1", data={"token": token})
    assert replay.status_code == 404
    assert "invalid" in replay.get_data(as_text=True).lower()
    assert store["bookings"][0]["booking_status"] == "cancelled"


def test_expired_token_fails(client, store):
    token, _ = _seed_reservation(
        store,
        expiry=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
    )
    get_resp = client.get(f"/cancel-reservation/BK-TEST1?token={token}")
    post_resp = client.post("/cancel-reservation/BK-TEST1", data={"token": token})
    assert get_resp.status_code == 404
    assert post_resp.status_code == 404
    assert store["bookings"][0]["booking_status"] == "confirmed"
    assert store["rpc_calls"] == []


def test_wrong_token_fails(client, store):
    token, _ = _seed_reservation(store)
    resp = client.get("/cancel-reservation/BK-TEST1?token=not-the-token")
    assert resp.status_code == 404
    assert store["bookings"][0]["booking_status"] == "confirmed"


def test_missing_token_fails(client, store):
    _seed_reservation(store)
    get_resp = client.get("/cancel-reservation/BK-TEST1")
    post_resp = client.post("/cancel-reservation/BK-TEST1", data={})
    assert get_resp.status_code == 404
    assert post_resp.status_code == 404
    assert store["rpc_calls"] == []


def test_confirmation_token_cannot_be_used_to_cancel(client, store):
    cancel_token, conf_token = _seed_reservation(store)
    resp = client.get(f"/cancel-reservation/BK-TEST1?token={conf_token}")
    assert resp.status_code == 404
    post = client.post("/cancel-reservation/BK-TEST1", data={"token": conf_token})
    assert post.status_code == 404
    assert store["bookings"][0]["booking_status"] == "confirmed"
    assert cancel_token != conf_token


def test_cancellation_token_cannot_view_confirmation_page(client, store, monkeypatch):
    cancel_token, conf_token = _seed_reservation(store)

    def fake_fetch(_sb, ref, token=None):
        if token == conf_token:
            from confirmation import LODG
            return {
                "booking_reference": ref,
                "access_token": conf_token,
                "lodge": LODG,
                "rooms": [],
            }
        return None

    monkeypatch.setattr(main, "fetch_confirmation_from_supabase", fake_fetch)
    bad = client.get(f"/reservation-confirmation/BK-TEST1?token={cancel_token}")
    good = client.get(f"/reservation-confirmation/BK-TEST1?token={conf_token}")
    assert bad.status_code == 404
    assert good.status_code == 200


def test_multi_room_reservation_cancels_every_row(client, store):
    token, _ = _seed_reservation(store, extra_rooms=1)
    assert len(store["bookings"]) == 2
    resp = client.post("/cancel-reservation/BK-TEST1", data={"token": token})
    assert resp.status_code == 200
    assert all(b["booking_status"] == "cancelled" for b in store["bookings"])
    assert all(b["cancelled_at"] for b in store["bookings"])
    assert store["cancellation"][0]["token_usage"] is True


def test_lodgeos_compatible_cancelled_status(client, store):
    token, _ = _seed_reservation(store)
    client.post("/cancel-reservation/BK-TEST1", data={"token": token})
    row = store["bookings"][0]
    assert row["booking_status"] == "cancelled"
    assert row["cancelled_at"] is not None
