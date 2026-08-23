"""Unit tests for pending_v7 HMAC cancellation capability.

Does not call Supabase, SMTP, or Moneris.
"""

from __future__ import annotations

import uuid

import pytest

import cancellation_capability
import main
from cancellation_capability import (
    INCOMPATIBLE,
    MATCH_CURRENT,
    MATCH_PREVIOUS,
    PLACEHOLDER,
    canonical_reservation_id,
    classify_stored_hash,
    configured_cancellation_token_secret,
    create_placeholder_hash,
    derive_cancellation_token,
    hash_cancellation_token,
)


SECRET = "k" * 32
RESERVATION = "11111111-2222-3333-4444-555555555555"
OTHER = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_same_reservation_and_secret_are_deterministic():
    first = derive_cancellation_token(SECRET, RESERVATION)
    second = derive_cancellation_token(SECRET, RESERVATION)
    assert first == second
    assert hash_cancellation_token(first) == hash_cancellation_token(second)
    assert len(hash_cancellation_token(first)) == 64


def test_different_reservations_differ():
    left = derive_cancellation_token(SECRET, RESERVATION)
    right = derive_cancellation_token(SECRET, OTHER)
    assert left != right
    assert hash_cancellation_token(left) != hash_cancellation_token(right)


def test_canonical_uuid_forms_match():
    raw = derive_cancellation_token(SECRET, RESERVATION)
    assert derive_cancellation_token(SECRET, RESERVATION.upper()) == raw
    assert derive_cancellation_token(SECRET, uuid.UUID(RESERVATION)) == raw
    assert (
        derive_cancellation_token(SECRET, RESERVATION.replace("-", "")) == raw
    )
    assert canonical_reservation_id(RESERVATION.upper()) == RESERVATION


def test_invalid_reservation_id_fails_closed():
    with pytest.raises(cancellation_capability.CancellationCapabilityError):
        derive_cancellation_token(SECRET, "not-a-uuid")


def test_hash_matches_existing_helper():
    raw = derive_cancellation_token(SECRET, RESERVATION)
    assert hash_cancellation_token(raw) == main._hash_cancellation_token(raw)
    assert hash_cancellation_token("") == main._hash_cancellation_token("")
    assert hash_cancellation_token("abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_classify_requires_exact_placeholder():
    current = hash_cancellation_token("current-token")
    previous = hash_cancellation_token("previous-token")
    placeholder = create_placeholder_hash(SECRET, "BK-ABC123")
    other = hash_cancellation_token("arbitrary-other-valid-token")
    assert classify_stored_hash(current, current) == MATCH_CURRENT
    assert (
        classify_stored_hash(
            placeholder, current, expected_placeholder_hash=placeholder
        )
        == PLACEHOLDER
    )
    assert (
        classify_stored_hash(other, current, expected_placeholder_hash=placeholder)
        == INCOMPATIBLE
    )
    assert classify_stored_hash(other, current) == INCOMPATIBLE
    assert classify_stored_hash(placeholder, current) == INCOMPATIBLE
    assert (
        classify_stored_hash(previous, current, previous_hash=previous)
        == MATCH_PREVIOUS
    )
    assert classify_stored_hash("not-a-hash", current) == INCOMPATIBLE
    assert classify_stored_hash(None, current) == INCOMPATIBLE


def test_placeholder_hash_is_deterministic_and_distinct():
    first = create_placeholder_hash(SECRET, "BK-ABC123")
    second = create_placeholder_hash(SECRET, "bk-abc123")
    other = create_placeholder_hash(SECRET, "BK-OTHER1")
    capability = hash_cancellation_token(
        derive_cancellation_token(SECRET, RESERVATION)
    )
    assert first == second
    assert first != other
    assert first != capability
    assert len(first) == 64


def test_missing_and_short_secret(monkeypatch):
    monkeypatch.delenv("CANCELLATION_TOKEN_SECRET", raising=False)
    assert configured_cancellation_token_secret() is None
    monkeypatch.setenv("CANCELLATION_TOKEN_SECRET", "s" * 31)
    assert configured_cancellation_token_secret() is None
    monkeypatch.setenv("CANCELLATION_TOKEN_SECRET", SECRET)
    assert configured_cancellation_token_secret() == SECRET


def test_does_not_reuse_other_secrets(monkeypatch):
    monkeypatch.delenv("CANCELLATION_TOKEN_SECRET", raising=False)
    monkeypatch.setenv("FLASK_SECRET_KEY", "f" * 32)
    monkeypatch.setenv("MONERIS_CLIENT_SECRET", "m" * 32)
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "b" * 32)
    monkeypatch.setenv("PAYMENT_QA_BYPASS_SECRET", "q" * 32)
    monkeypatch.setenv("PAYMENT_EXPIRY_CRON_SECRET", "c" * 32)
    monkeypatch.setenv("PAYMENT_RECONCILIATION_ADMIN_SECRET", "a" * 32)
    assert configured_cancellation_token_secret() is None
