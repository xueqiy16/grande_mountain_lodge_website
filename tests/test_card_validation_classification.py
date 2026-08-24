"""Card Validation non-2xx classification. Moneris HTTP is always mocked."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import Mock, patch
from uuid import UUID

import pytest
import requests

import main
from payment_api.config import (
    PRODUCTION_API_BASE_URL,
    PRODUCTION_HOSTED_TOKENIZATION_URL,
    REQUIRED_API_VERSION,
    SANDBOX_API_BASE_URL,
    SANDBOX_HOSTED_TOKENIZATION_URL,
    load_config,
)
from payment_api.credential_registration import register_booking_payment_credential
from payment_api.credential_repository import (
    ERROR_INVALID_RESPONSE,
    ERROR_PROCESSOR_UNAVAILABLE,
    ERROR_VALIDATION_REJECTED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RECONCILIATION_REQUIRED,
    CredentialRecord,
)
from payment_api.errors import (
    CredentialReconciliationRequiredError,
    CredentialRegistrationError,
    MonerisValidationError,
)
from payment_api.validation import validate_card


UNIQUE_ACCESS_TOKEN = "UNIQUE_ACCESS_TOKEN_VALUE_XYZ"
UNIQUE_DATA_KEY = "ot-UNIQUE_DATA_KEY_VALUE12"
UNIQUE_PAYMENT_METHOD_ID = "pm01FAKEPAYMENTMETHODID00000001"
UNIQUE_ISSUER_ID = "ISSUEIDUNIQUE1"
UNIQUE_CLIENT_SECRET = "UNIQUE_CLIENT_SECRET_VALUE_XYZ"
FIXED_IDEMPOTENCY_KEY = "11111111-2222-4333-8444-555555555555"
BOOKING_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
CREDENTIAL_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
RAW_VALIDATION_BODY = (
    '{"paymentMethodId":"pm01FAKEPAYMENTMETHODID00000001",'
    '"temporaryToken":"ot-UNIQUE_DATA_KEY_VALUE12",'
    '"access_token":"UNIQUE_ACCESS_TOKEN_VALUE_XYZ",'
    '"cardNumber":"4242424242424242"}'
)
SENTINELS = (
    UNIQUE_DATA_KEY,
    UNIQUE_ACCESS_TOKEN,
    UNIQUE_PAYMENT_METHOD_ID,
    UNIQUE_ISSUER_ID,
    UNIQUE_CLIENT_SECRET,
    "UNIQUE_SUPABASE_SERVICE_ROLE_KEY_XYZ",
    RAW_VALIDATION_BODY,
    "4242424242424242",
    FIXED_IDEMPOTENCY_KEY,
)


def _sandbox_env():
    return {
        "MONERIS_ENV": "sandbox",
        "MONERIS_CLIENT_ID": "test-client-id",
        "MONERIS_CLIENT_SECRET": UNIQUE_CLIENT_SECRET,
        "MONERIS_MERCHANT_ID": "test-merchant-id",
        "MONERIS_API_VERSION": REQUIRED_API_VERSION,
        "MONERIS_API_BASE_URL": SANDBOX_API_BASE_URL,
        "MONERIS_HOSTED_TOKENIZATION_PROFILE_ID": "test-profile-id",
        "MONERIS_HOSTED_TOKENIZATION_URL": SANDBOX_HOSTED_TOKENIZATION_URL,
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_SERVICE_ROLE_KEY": "UNIQUE_SUPABASE_SERVICE_ROLE_KEY_XYZ",
    }


def _config():
    return load_config(_sandbox_env())


def _call_validate():
    return validate_card(_config(), UNIQUE_DATA_KEY, FIXED_IDEMPOTENCY_KEY)


def _ok_payload(**overrides):
    payload = {
        "validationId": "va01FAKEVALIDATIONID00000000001",
        "merchantId": "test-merchant-id",
        "createdAt": "2019-07-30T06:43:40.252Z",
        "validationStatus": "SUCCEEDED",
        "transactionDetails": {},
        "verificationDetails": {},
        "paymentMethod": {
            "paymentMethodId": UNIQUE_PAYMENT_METHOD_ID,
            "merchantId": "test-merchant-id",
            "createdAt": "2019-07-30T06:43:40.252Z",
            "paymentMethodInformation": {
                "paymentMethodType": "CARD",
                "paymentMethodSource": "TEMPORARY_TOKEN",
                "storePaymentMethod": "MERCHANT_INITIATED",
                "cardInformation": {
                    "lastFour": "4242",
                    "cardBrand": "VISA",
                    "expiryMonth": 12,
                    "expiryYear": 2028,
                },
            },
        },
        "credentialOnFileResponse": {"issuerId": UNIQUE_ISSUER_ID},
    }
    payload.update(overrides)
    return payload


def _response(status_code=201, payload=None, text="", json_error=False):
    response = Mock()
    response.status_code = status_code
    response.ok = 200 <= status_code < 400
    response.text = text
    if json_error:
        response.json.side_effect = ValueError("No JSON")
    elif payload is not None:
        response.json.return_value = payload
    else:
        response.json.side_effect = ValueError("No JSON")
    return response


def _assert_safe(exc):
    rendered = f"{exc!s}{exc!r}"
    for sentinel in SENTINELS:
        assert sentinel not in rendered, sentinel
    assert "declined" not in rendered.lower() or "HTTP" in rendered


def _raise_from(mock_post, **response_kwargs):
    mock_post.return_value = _response(**response_kwargs)
    with pytest.raises(MonerisValidationError) as ctx:
        _call_validate()
    _assert_safe(ctx.value)
    return ctx.value


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr(
        "payment_api.validation.get_access_token",
        lambda _config: UNIQUE_ACCESS_TOKEN,
    )


def test_direct_bookings_remain_paused():
    assert main.DIRECT_BOOKINGS_PAUSED is True


def test_declined_error_category_is_confirmed_decline():
    with patch("payment_api.validation.requests.post") as mock_post:
        exc = _raise_from(
            mock_post,
            status_code=409,
            payload={"category": "DECLINED_ERROR", "detail": "issuer said no"},
        )
    assert exc.category == MonerisValidationError.CONFIRMED_DECLINE
    assert "issuer said no" not in f"{exc!s}{exc!r}"


def test_http_422_declined_error_is_confirmed_decline():
    with patch("payment_api.validation.requests.post") as mock_post:
        exc = _raise_from(
            mock_post,
            status_code=422,
            payload={"category": "DECLINED_ERROR", "title": "EXCESS PIN TRIES"},
        )
    assert exc.category == MonerisValidationError.CONFIRMED_DECLINE


def test_http_400_declined_error_is_confirmed_decline():
    with patch("payment_api.validation.requests.post") as mock_post:
        exc = _raise_from(
            mock_post,
            status_code=400,
            payload={"category": "DECLINED_ERROR"},
        )
    assert exc.category == MonerisValidationError.CONFIRMED_DECLINE


def test_invalid_request_error_category_is_invalid_request():
    with patch("payment_api.validation.requests.post") as mock_post:
        exc = _raise_from(
            mock_post,
            status_code=400,
            payload={"category": "INVALID_REQUEST_ERROR"},
        )
    assert exc.category == MonerisValidationError.INVALID_REQUEST


def test_unknown_category_with_declined_detail_is_not_confirmed_decline():
    with patch("payment_api.validation.requests.post") as mock_post:
        exc = _raise_from(
            mock_post,
            status_code=422,
            payload={"category": "API_ERROR", "detail": "declined", "title": "rejected"},
        )
    assert exc.category == MonerisValidationError.PROCESSOR_UNAVAILABLE
    assert exc.category != MonerisValidationError.CONFIRMED_DECLINE


def test_http_400_without_category_is_processor_unavailable():
    with patch("payment_api.validation.requests.post") as mock_post:
        exc = _raise_from(mock_post, status_code=400, payload={"title": "declined"})
    assert exc.category == MonerisValidationError.PROCESSOR_UNAVAILABLE


def test_http_500_with_decline_words_is_processor_unavailable():
    with patch("payment_api.validation.requests.post") as mock_post:
        exc = _raise_from(
            mock_post,
            status_code=500,
            payload={
                "category": "DECLINED_ERROR",
                "detail": "declined",
                "title": "rejected",
            },
        )
    assert exc.category == MonerisValidationError.PROCESSOR_UNAVAILABLE


def test_malformed_non_2xx_is_processor_unavailable():
    with patch("payment_api.validation.requests.post") as mock_post:
        exc = _raise_from(
            mock_post,
            status_code=400,
            text=RAW_VALIDATION_BODY,
            json_error=True,
        )
    assert exc.category == MonerisValidationError.PROCESSOR_UNAVAILABLE


def test_non_object_json_non_2xx_is_processor_unavailable():
    with patch("payment_api.validation.requests.post") as mock_post:
        exc = _raise_from(mock_post, status_code=422, payload=["declined"])
    assert exc.category == MonerisValidationError.PROCESSOR_UNAVAILABLE


def test_timeout_is_processor_unavailable():
    with patch("payment_api.validation.requests.post", side_effect=requests.Timeout()):
        with pytest.raises(MonerisValidationError) as ctx:
            _call_validate()
    assert ctx.value.category == MonerisValidationError.PROCESSOR_UNAVAILABLE
    _assert_safe(ctx.value)


def test_connection_error_is_processor_unavailable():
    with patch(
        "payment_api.validation.requests.post",
        side_effect=requests.ConnectionError(),
    ):
        with pytest.raises(MonerisValidationError) as ctx:
            _call_validate()
    assert ctx.value.category == MonerisValidationError.PROCESSOR_UNAVAILABLE
    _assert_safe(ctx.value)


def test_2xx_declined_validation_status_is_confirmed_decline():
    with patch("payment_api.validation.requests.post") as mock_post:
        mock_post.return_value = _response(
            payload=_ok_payload(validationStatus="DECLINED")
        )
        with pytest.raises(MonerisValidationError) as ctx:
            _call_validate()
    assert ctx.value.category == MonerisValidationError.CONFIRMED_DECLINE
    _assert_safe(ctx.value)


def test_2xx_declined_retry_validation_status_is_confirmed_decline():
    with patch("payment_api.validation.requests.post") as mock_post:
        mock_post.return_value = _response(
            payload=_ok_payload(validationStatus="DECLINED_RETRY")
        )
        with pytest.raises(MonerisValidationError) as ctx:
            _call_validate()
    assert ctx.value.category == MonerisValidationError.CONFIRMED_DECLINE
    _assert_safe(ctx.value)


def test_2xx_succeeded_still_succeeds():
    with patch("payment_api.validation.requests.post") as mock_post:
        mock_post.return_value = _response(payload=_ok_payload())
        result = _call_validate()
    assert result.card_brand == "VISA"
    rendered = f"{result!s}{result!r}"
    assert UNIQUE_PAYMENT_METHOD_ID not in rendered
    assert UNIQUE_ISSUER_ID not in rendered
    assert UNIQUE_DATA_KEY not in rendered


def test_request_body_unchanged_and_has_no_charge_fields():
    with patch("payment_api.validation.requests.post") as mock_post:
        mock_post.return_value = _response(payload=_ok_payload())
        _call_validate()
    body = mock_post.call_args.kwargs["json"]
    assert body["paymentMethod"]["storePaymentMethod"] == "MERCHANT_INITIATED"
    cof = body["paymentMethod"]["credentialOnFileInformation"]
    assert cof["paymentIndicator"] == "UNSCHEDULED_CREDENTIAL_ON_FILE"
    assert cof["paymentInformation"] == "FIRST"
    assert "/payments" not in mock_post.call_args.args[0]
    assert mock_post.call_args.args[0].endswith("/validations")
    assert "amount" not in body
    assert "automaticCapture" not in body
    assert "purchase" not in body


def test_pinned_urls_and_api_version_unchanged():
    assert REQUIRED_API_VERSION == "2026-08-14"
    assert SANDBOX_API_BASE_URL == "https://api.sb.moneris.io"
    assert PRODUCTION_API_BASE_URL == "https://api.moneris.io"
    assert SANDBOX_HOSTED_TOKENIZATION_URL.endswith("/HPPtoken/index.php")
    assert PRODUCTION_HOSTED_TOKENIZATION_URL.endswith("/HPPtoken/index.php")


class _MiniRepo:
    def __init__(self):
        self.record = CredentialRecord(
            credential_id=CREDENTIAL_ID,
            booking_id=BOOKING_ID,
            registration_idempotency_key=UUID(FIXED_IDEMPOTENCY_KEY),
            registration_status=STATUS_PENDING,
            registration_error_category=None,
            moneris_payment_method_id=None,
            moneris_issuer_id=None,
            card_brand=None,
            last_four=None,
            expiry_month=None,
            expiry_year=None,
            registered_at=None,
        )
        self.failed_calls = []
        self.reconciliation_calls = []

    def get_registration_by_idempotency_key(self, _key):
        return None

    def get_active_registration_for_booking(self, _booking_id):
        return None

    def begin_registration(self, _booking_id, _key):
        return self.record

    def mark_registration_failed(self, credential_id, error_category):
        self.failed_calls.append((credential_id, error_category))
        self.record = replace(
            self.record,
            registration_status=STATUS_FAILED,
            registration_error_category=error_category,
        )
        return self.record

    def mark_reconciliation_required(self, credential_id, error_category):
        self.reconciliation_calls.append((credential_id, error_category))
        self.record = replace(
            self.record,
            registration_status=STATUS_RECONCILIATION_REQUIRED,
            registration_error_category=error_category,
        )
        return self.record


def _register_with(exc, repo):
    with patch(
        "payment_api.credential_registration.validate_card",
        side_effect=exc,
    ):
        return register_booking_payment_credential(
            _config(),
            BOOKING_ID,
            UNIQUE_DATA_KEY,
            FIXED_IDEMPOTENCY_KEY,
            repository=repo,
        )


def test_confirmed_decline_marks_failed_validation_rejected():
    repo = _MiniRepo()
    with pytest.raises(CredentialRegistrationError) as ctx:
        _register_with(
            MonerisValidationError(
                "Moneris Card Validation was rejected",
                category=MonerisValidationError.CONFIRMED_DECLINE,
            ),
            repo,
        )
    assert not isinstance(ctx.value, CredentialReconciliationRequiredError)
    assert repo.failed_calls == [(CREDENTIAL_ID, ERROR_VALIDATION_REJECTED)]
    assert repo.record.registration_status == STATUS_FAILED
    _assert_safe(ctx.value)


def test_processor_unavailable_raises_typed_reconciliation_error():
    repo = _MiniRepo()
    with pytest.raises(CredentialReconciliationRequiredError) as ctx:
        _register_with(
            MonerisValidationError(
                "Moneris Card Validation request failed",
                category=MonerisValidationError.PROCESSOR_UNAVAILABLE,
            ),
            repo,
        )
    assert repo.reconciliation_calls == [(CREDENTIAL_ID, ERROR_PROCESSOR_UNAVAILABLE)]
    assert repo.record.registration_status == STATUS_RECONCILIATION_REQUIRED
    _assert_safe(ctx.value)


def test_invalid_response_raises_typed_reconciliation_error():
    repo = _MiniRepo()
    with pytest.raises(CredentialReconciliationRequiredError) as ctx:
        _register_with(
            MonerisValidationError(
                "Moneris Card Validation returned an invalid response",
                category=MonerisValidationError.INVALID_RESPONSE,
            ),
            repo,
        )
    assert repo.reconciliation_calls == [(CREDENTIAL_ID, ERROR_INVALID_RESPONSE)]
    assert repo.record.registration_status == STATUS_RECONCILIATION_REQUIRED
    _assert_safe(ctx.value)


def test_invalid_request_does_not_mark_failed_or_held():
    repo = _MiniRepo()
    with pytest.raises(CredentialRegistrationError) as ctx:
        _register_with(
            MonerisValidationError(
                "Card validation dataKey is invalid",
                category=MonerisValidationError.INVALID_REQUEST,
            ),
            repo,
        )
    assert not isinstance(ctx.value, CredentialReconciliationRequiredError)
    assert repo.failed_calls == []
    assert repo.reconciliation_calls == []
    assert repo.record.registration_status == STATUS_PENDING
    _assert_safe(ctx.value)
