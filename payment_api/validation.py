"""Server-internal Moneris Card Validation client.

Hosted Tokenization returns a temporary token commonly called dataKey.
The current Moneris Create Card Validation API accepts that value as
``paymentMethod.temporaryToken`` with ``paymentMethodSource=TEMPORARY_TOKEN``.

This call also registers a merchant-initiated credential on file so later
authorized no-show/cancellation charges can use ``paymentMethodId`` plus
``issuerId``. It does not capture a payment.

``idempotencyKey`` is caller-owned. ``validate_card`` never generates a
UUID. The same caller key must be sent unchanged on retries.

This module does not expose an HTTP route, does not persist the dataKey,
and does not call Hosted Tokenization or Supabase.

OAuth failures from ``get_access_token`` propagate as ``MonerisAuthError``
so callers can distinguish authentication failure from validation failure.
Those messages are already safe and do not include tokens.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Optional

import requests

from payment_api.auth import OAUTH_TIMEOUT, get_access_token
from payment_api.config import PaymentConfig
from payment_api.errors import MonerisValidationError

VALIDATIONS_PATH = "/validations"
PAYMENT_METHOD_SOURCE_TEMPORARY_TOKEN = "TEMPORARY_TOKEN"
STORE_PAYMENT_METHOD_MERCHANT_INITIATED = "MERCHANT_INITIATED"
COF_PAYMENT_INDICATOR_FIRST = "UNSCHEDULED_CREDENTIAL_ON_FILE"
COF_PAYMENT_INFORMATION_FIRST = "FIRST"
VALIDATION_STATUS_SUCCEEDED = "SUCCEEDED"
VALIDATION_STATUS_DECLINED = frozenset({"DECLINED", "DECLINED_RETRY"})
# Current Unified API token schema: minLength 25, maxLength 28.
TEMPORARY_TOKEN_MIN_LENGTH = 25
TEMPORARY_TOKEN_MAX_LENGTH = 28
ISSUER_ID_MAX_LENGTH = 15
_REDACTED = "<redacted>"


@dataclass(frozen=True)
class CardValidationResult:
    """Persistent identifiers and safe card metadata from Card Validation.

    Does not include the dataKey, OAuth token, PAN, CVD, or raw response.
    """

    payment_method_id: str
    issuer_id: str
    card_brand: Optional[str]
    last_four: Optional[str]
    expiry_month: Optional[int]
    expiry_year: Optional[int]

    def __repr__(self) -> str:
        return (
            "CardValidationResult("
            f"payment_method_id={_REDACTED}, "
            f"issuer_id={_REDACTED}, "
            f"card_brand={self.card_brand!r}, "
            f"last_four={self.last_four!r}, "
            f"expiry_month={self.expiry_month!r}, "
            f"expiry_year={self.expiry_year!r})"
        )

    __str__ = __repr__


def validate_card(
    config: PaymentConfig,
    data_key: object,
    idempotency_key: object,
) -> CardValidationResult:
    """Validate a Hosted Tokenization token and store it as a CoF credential.

    ``data_key`` is the opaque HT token. It is sent as ``temporaryToken``.
    ``idempotency_key`` is the caller-owned UUID sent as ``idempotencyKey``.
    """
    token = _require_data_key(data_key)
    key = _require_idempotency_key(idempotency_key)
    access_token = get_access_token(config)
    payload = _request_validation(config, access_token, token, key)
    return _parse_validation_result(payload)


def _require_idempotency_key(idempotency_key: object) -> str:
    if isinstance(idempotency_key, uuid.UUID):
        return str(idempotency_key)
    if not isinstance(idempotency_key, str):
        raise MonerisValidationError(
            "Card validation idempotency key is invalid",
            category=MonerisValidationError.INVALID_REQUEST,
        )
    stripped = idempotency_key.strip()
    try:
        parsed = uuid.UUID(stripped)
    except ValueError:
        raise MonerisValidationError(
            "Card validation idempotency key is invalid",
            category=MonerisValidationError.INVALID_REQUEST,
        ) from None
    return str(parsed)


def _require_data_key(data_key: object) -> str:
    if not isinstance(data_key, str):
        raise MonerisValidationError(
            "Card validation dataKey is invalid",
            category=MonerisValidationError.INVALID_REQUEST,
        )
    token = data_key.strip()
    if not (
        TEMPORARY_TOKEN_MIN_LENGTH <= len(token) <= TEMPORARY_TOKEN_MAX_LENGTH
    ):
        raise MonerisValidationError(
            "Card validation dataKey is invalid",
            category=MonerisValidationError.INVALID_REQUEST,
        )
    return token


def _request_validation(
    config: PaymentConfig,
    access_token: str,
    temporary_token: str,
    idempotency_key: str,
) -> dict[str, Any]:
    url = f"{config.moneris_api_base_url}{VALIDATIONS_PATH}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Api-Version": config.moneris_api_version,
        "X-Merchant-Id": config.moneris_merchant_id,
    }
    body = {
        "idempotencyKey": idempotency_key,
        "paymentMethod": {
            "paymentMethodSource": PAYMENT_METHOD_SOURCE_TEMPORARY_TOKEN,
            "temporaryToken": temporary_token,
            "storePaymentMethod": STORE_PAYMENT_METHOD_MERCHANT_INITIATED,
            "credentialOnFileInformation": {
                "paymentIndicator": COF_PAYMENT_INDICATOR_FIRST,
                "paymentInformation": COF_PAYMENT_INFORMATION_FIRST,
            },
        },
    }
    try:
        response = requests.post(
            url,
            json=body,
            headers=headers,
            timeout=OAUTH_TIMEOUT,
        )
    except requests.RequestException:
        raise MonerisValidationError(
            "Moneris Card Validation request failed",
            category=MonerisValidationError.PROCESSOR_UNAVAILABLE,
        ) from None

    if not (200 <= response.status_code < 300):
        _raise_non_2xx_validation_error(response)
    return _parse_json_object(response)


PROBLEM_JSON_DECLINED = "DECLINED_ERROR"
PROBLEM_JSON_INVALID_REQUEST = "INVALID_REQUEST_ERROR"


def _raise_non_2xx_validation_error(response: requests.Response) -> None:
    """Classify a non-2xx Card Validation response. Never reads message text."""
    status_code = response.status_code
    message = _http_error_message(status_code)
    if isinstance(status_code, int) and status_code >= 500:
        raise MonerisValidationError(
            message,
            category=MonerisValidationError.PROCESSOR_UNAVAILABLE,
        )
    category = _problem_json_category(response)
    if category == PROBLEM_JSON_DECLINED:
        raise MonerisValidationError(
            message,
            category=MonerisValidationError.CONFIRMED_DECLINE,
        )
    if category == PROBLEM_JSON_INVALID_REQUEST:
        raise MonerisValidationError(
            message,
            category=MonerisValidationError.INVALID_REQUEST,
        )
    raise MonerisValidationError(
        message,
        category=MonerisValidationError.PROCESSOR_UNAVAILABLE,
    )


def _problem_json_category(response: requests.Response) -> Optional[str]:
    """Return the documented problem+json category, or None if unreadable.

    Does not inspect title, detail, or errorMessage. Does not require a
    Content-Type header (mocked responses may omit it).
    """
    try:
        payload = response.json()
    except (ValueError, TypeError, AttributeError):
        return None
    if not isinstance(payload, dict):
        return None
    raw = payload.get("category")
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    return value or None


def _http_error_message(status_code: int) -> str:
    if 400 <= status_code < 500:
        return f"Moneris Card Validation was rejected (HTTP {status_code})"
    return f"Moneris Card Validation request failed (HTTP {status_code})"


def _parse_json_object(response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        raise MonerisValidationError(
            "Moneris Card Validation returned an invalid response",
            category=MonerisValidationError.INVALID_RESPONSE,
        ) from None
    if not isinstance(payload, dict):
        raise MonerisValidationError(
            "Moneris Card Validation returned an invalid response",
            category=MonerisValidationError.INVALID_RESPONSE,
        )
    return payload


def _parse_validation_result(payload: dict[str, Any]) -> CardValidationResult:
    _require_succeeded_status(payload.get("validationStatus"))

    payment_method = payload.get("paymentMethod")
    if not isinstance(payment_method, dict):
        raise MonerisValidationError(
            "Moneris Card Validation returned an invalid response",
            category=MonerisValidationError.INVALID_RESPONSE,
        )

    payment_method_id = _require_non_empty_string(
        payment_method.get("paymentMethodId")
    )
    issuer_id = _require_issuer_id(payload.get("credentialOnFileResponse"))
    card_info = _card_information(payment_method)
    return CardValidationResult(
        payment_method_id=payment_method_id,
        issuer_id=issuer_id,
        card_brand=_optional_card_brand(card_info),
        last_four=_optional_last_four(card_info),
        expiry_month=_optional_expiry_month(card_info),
        expiry_year=_optional_expiry_year(card_info),
    )


def _require_succeeded_status(status: object) -> None:
    if not isinstance(status, str) or not status.strip():
        raise MonerisValidationError(
            "Moneris Card Validation returned an invalid response",
            category=MonerisValidationError.INVALID_RESPONSE,
        )
    normalized = status.strip()
    if normalized == VALIDATION_STATUS_SUCCEEDED:
        return
    if normalized in VALIDATION_STATUS_DECLINED:
        raise MonerisValidationError(
            "Moneris Card Validation was rejected",
            category=MonerisValidationError.CONFIRMED_DECLINE,
        )
    raise MonerisValidationError(
        "Moneris Card Validation returned an invalid response",
        category=MonerisValidationError.INVALID_RESPONSE,
    )


def _require_non_empty_string(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MonerisValidationError(
            "Moneris Card Validation returned an invalid response",
            category=MonerisValidationError.INVALID_RESPONSE,
        )
    return value.strip()


def _require_issuer_id(credential_on_file: object) -> str:
    if not isinstance(credential_on_file, dict):
        raise MonerisValidationError(
            "Moneris Card Validation returned an invalid response",
            category=MonerisValidationError.INVALID_RESPONSE,
        )
    value = credential_on_file.get("issuerId")
    if not isinstance(value, str):
        raise MonerisValidationError(
            "Moneris Card Validation returned an invalid response",
            category=MonerisValidationError.INVALID_RESPONSE,
        )
    issuer_id = value.strip()
    if not issuer_id or len(issuer_id) > ISSUER_ID_MAX_LENGTH:
        raise MonerisValidationError(
            "Moneris Card Validation returned an invalid response",
            category=MonerisValidationError.INVALID_RESPONSE,
        )
    return issuer_id


def _card_information(payment_method: dict[str, Any]) -> dict[str, Any]:
    information = payment_method.get("paymentMethodInformation")
    if not isinstance(information, dict):
        return {}
    card_info = information.get("cardInformation")
    if not isinstance(card_info, dict):
        return {}
    return card_info


def _optional_card_brand(card_info: dict[str, Any]) -> Optional[str]:
    value = card_info.get("cardBrand")
    if not isinstance(value, str):
        return None
    brand = value.strip()
    return brand or None


def _optional_last_four(card_info: dict[str, Any]) -> Optional[str]:
    value = card_info.get("lastFour")
    if not isinstance(value, str):
        return None
    last_four = value.strip()
    if len(last_four) != 4:
        return None
    return last_four


def _optional_expiry_month(card_info: dict[str, Any]) -> Optional[int]:
    value = card_info.get("expiryMonth")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 1 or value > 12:
        return None
    return value


def _optional_expiry_year(card_info: dict[str, Any]) -> Optional[int]:
    value = card_info.get("expiryYear")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 2022 or value > 9999:
        return None
    return value
