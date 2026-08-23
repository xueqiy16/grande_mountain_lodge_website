"""Import-only checks for the vendored payment_api library.

These tests must not call Moneris or live Supabase. Website completion
tests mock register_credential_fn; this file proves the default registrar
can import payment_api without that mock.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

import payment_completion
from payment_completion import PaymentCompletionError


LIBRARY_FILES = (
    "__init__.py",
    "errors.py",
    "config.py",
    "auth.py",
    "validation.py",
    "credential_repository.py",
    "credential_registration.py",
)
EXCLUDED_FILES = (
    "flask_app.py",
    "qa_card_validation.py",
    "payment_session_state.py",
    "reconciliation_recovery.py",
)
WEBSITE_PACKAGE = Path(__file__).resolve().parents[1] / "payment_api"
PAYMENTS_API_PACKAGE = (
    Path(__file__).resolve().parents[2]
    / "grande_mountain_lodge_payments_api"
    / "payment_api"
)


def _forbid_network(monkeypatch):
    def fail(*_args, **_kwargs):
        raise AssertionError("network request is forbidden in import tests")

    monkeypatch.setattr(requests, "request", fail)
    monkeypatch.setattr(requests, "post", fail)
    monkeypatch.setattr(requests, "get", fail)
    monkeypatch.setattr(requests, "put", fail)
    monkeypatch.setattr(requests, "patch", fail)
    monkeypatch.setattr(requests, "delete", fail)


def test_vendored_library_excludes_server_and_harness_modules():
    for name in LIBRARY_FILES:
        assert (WEBSITE_PACKAGE / name).is_file(), name
    for name in EXCLUDED_FILES:
        assert not (WEBSITE_PACKAGE / name).exists(), name
    assert not (WEBSITE_PACKAGE / ".env").exists()
    assert not list(WEBSITE_PACKAGE.glob(".env*"))


@pytest.mark.skipif(
    not PAYMENTS_API_PACKAGE.is_dir(),
    reason="payments API checkout not present",
)
def test_vendored_library_matches_payments_api_source():
    for name in LIBRARY_FILES:
        website = (WEBSITE_PACKAGE / name).read_bytes()
        source = (PAYMENTS_API_PACKAGE / name).read_bytes()
        assert website == source, name


def test_payment_api_modules_import_without_network(monkeypatch):
    _forbid_network(monkeypatch)
    import payment_api.config
    import payment_api.auth
    import payment_api.validation
    import payment_api.credential_repository
    import payment_api.credential_registration
    import payment_completion as completion

    assert payment_api.config.load_config is not None
    assert payment_api.auth.get_access_token is not None
    assert payment_api.validation.validate_card is not None
    assert payment_api.credential_repository.CredentialRepository is not None
    assert (
        payment_api.credential_registration.register_booking_payment_credential
        is not None
    )
    assert completion.default_register_booking_payment_credential is not None


def test_default_registrar_imports_without_mocked_register_fn(monkeypatch):
    _forbid_network(monkeypatch)
    assert payment_completion.register_credential_fn is None
    from payment_api.credential_registration import (
        register_booking_payment_credential,
    )

    loaded = {}

    def fake_load(environ=None):
        loaded["called"] = True
        loaded["environ_is_mapping"] = isinstance(environ, dict)
        return SimpleNamespace(name="payment-config")

    def fake_register(config, booking_id, data_key, idempotency_key):
        loaded["register"] = (
            config.name,
            booking_id,
            data_key,
            idempotency_key,
        )
        return SimpleNamespace(registration_status="SUCCEEDED")

    monkeypatch.setattr("payment_api.config.load_config", fake_load)
    monkeypatch.setattr(
        "payment_api.credential_registration.register_booking_payment_credential",
        fake_register,
    )
    result = payment_completion.default_register_booking_payment_credential(
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "K" * 26,
        "99999999-8888-7777-6666-555555555555",
    )
    assert loaded["called"] is True
    assert result.registration_status == "SUCCEEDED"
    assert loaded["register"][0] == "payment-config"
    assert register_booking_payment_credential is not None


def test_default_registrar_missing_config_is_unavailable_without_network(
    monkeypatch, caplog
):
    _forbid_network(monkeypatch)
    for name in (
        "MONERIS_ENV",
        "MONERIS_CLIENT_ID",
        "MONERIS_CLIENT_SECRET",
        "MONERIS_MERCHANT_ID",
        "MONERIS_API_BASE_URL",
        "MONERIS_API_VERSION",
        "MONERIS_HOSTED_TOKENIZATION_PROFILE_ID",
        "MONERIS_HOSTED_TOKENIZATION_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
        "SUPABASE_SECRET_KEY",
        "SUPABASE_KEY",
        "SUPABASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    with caplog.at_level(logging.ERROR):
        with pytest.raises(PaymentCompletionError) as ctx:
            payment_completion.default_register_booking_payment_credential(
                "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "K" * 26,
                "99999999-8888-7777-6666-555555555555",
            )
    assert ctx.value.status == 503
    assert "Payment is not available." in ctx.value.user_message
    assert "MONERIS_CLIENT_SECRET" not in caplog.text
    assert "SUPABASE_SERVICE_ROLE_KEY" not in caplog.text


def test_website_startup_import_does_not_require_moneris_config(monkeypatch):
    for name in (
        "MONERIS_ENV",
        "MONERIS_CLIENT_ID",
        "MONERIS_CLIENT_SECRET",
        "MONERIS_MERCHANT_ID",
        "MONERIS_API_BASE_URL",
        "MONERIS_HOSTED_TOKENIZATION_PROFILE_ID",
        "MONERIS_HOSTED_TOKENIZATION_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    import main

    assert main.DIRECT_BOOKINGS_PAUSED is True
    assert payment_completion.register_credential_fn is None
