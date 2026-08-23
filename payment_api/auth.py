"""In-process Moneris OAuth client-credentials helper.

This module obtains Bearer access tokens for future Moneris API calls.
It does not implement card validation or payment capture.

Token caching is an in-process optimization only. A Vercel serverless
instance may be cold-started or replaced at any time, in which case a new
token is requested. The lock below serializes concurrent callers inside
one process; it does not coordinate separate serverless instances.

Cache identity is a SHA-256 digest of MONERIS_ENV, MONERIS_API_BASE_URL,
MONERIS_CLIENT_ID, and MONERIS_CLIENT_SECRET. Merchant ID is not sent to
the OAuth token endpoint and is not part of the fingerprint.
"""

from __future__ import annotations

import hashlib
import math
import threading
from dataclasses import dataclass
from typing import Any, Optional

import requests

from payment_api.config import PaymentConfig
from payment_api.errors import MonerisAuthError

OAUTH_TOKEN_PATH = "/oauth2/token"
OAUTH_GRANT_TYPE = "client_credentials"
OAUTH_SCOPE = "payment.write"
# requests timeout=(connect, read). This is not a guaranteed total
# wall-clock deadline for the request; DNS, TLS, and library behavior can
# extend observed duration. Do not derive PROCESSING_TIMEOUT from this.
OAUTH_TIMEOUT = (5, 15)
_MAX_EXPIRY_BUFFER_SECONDS = 60.0

_cache_lock = threading.Lock()
_token_cache: Optional["_CachedToken"] = None


@dataclass(frozen=True)
class _CachedToken:
    access_token: str
    refresh_at: float
    config_id: str


def get_access_token(config: PaymentConfig) -> str:
    """Return a usable Moneris OAuth access token for ``config``.

    Callers receive the access_token string (for ``Authorization: Bearer``).
    Caching and OAuth request details stay inside this module.
    """
    config_id = _config_fingerprint(config)
    now = _now()
    cached = _usable_cached_token(config_id, now)
    if cached is not None:
        return cached

    with _cache_lock:
        now = _now()
        cached = _usable_cached_token(config_id, now)
        if cached is not None:
            return cached
        return _fetch_and_store_token(config, config_id, now)


def _reset_token_cache_for_tests() -> None:
    """Drop the in-process token cache. Does not return cache contents."""
    global _token_cache
    with _cache_lock:
        _token_cache = None


def _now() -> float:
    import time

    return time.monotonic()


def _config_fingerprint(config: PaymentConfig) -> str:
    digest = hashlib.sha256()
    for part in (
        config.moneris_env,
        config.moneris_api_base_url,
        config.moneris_client_id,
        config.moneris_client_secret,
    ):
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _usable_cached_token(config_id: str, now: float) -> Optional[str]:
    cached = _token_cache
    if cached is None:
        return None
    if cached.config_id != config_id:
        return None
    if now >= cached.refresh_at:
        return None
    return cached.access_token


def _fetch_and_store_token(
    config: PaymentConfig,
    config_id: str,
    now: float,
) -> str:
    global _token_cache
    access_token, expires_in = _request_access_token(config)
    refresh_at = _refresh_at(now, expires_in)
    if refresh_at <= now:
        raise MonerisAuthError("Moneris OAuth returned an invalid response")
    _token_cache = _CachedToken(
        access_token=access_token,
        refresh_at=refresh_at,
        config_id=config_id,
    )
    return access_token


def _refresh_at(now: float, expires_in: float) -> float:
    buffer = min(_MAX_EXPIRY_BUFFER_SECONDS, expires_in * 0.5)
    return now + expires_in - buffer


def _request_access_token(config: PaymentConfig) -> tuple[str, float]:
    token_url = f"{config.moneris_api_base_url}{OAUTH_TOKEN_PATH}"
    try:
        response = requests.post(
            token_url,
            data={
                "grant_type": OAUTH_GRANT_TYPE,
                "client_id": config.moneris_client_id,
                "client_secret": config.moneris_client_secret,
                "scope": OAUTH_SCOPE,
            },
            timeout=OAUTH_TIMEOUT,
        )
    except requests.RequestException:
        raise MonerisAuthError("Moneris OAuth request failed") from None

    if not (200 <= response.status_code < 300):
        raise MonerisAuthError(
            _http_error_message(response.status_code)
        )

    payload = _parse_json_object(response)
    access_token = _require_access_token(payload)
    _require_bearer_token_type(payload)
    expires_in = _require_expires_in(payload)
    return access_token, expires_in


def _http_error_message(status_code: int) -> str:
    if 400 <= status_code < 500:
        return f"Moneris OAuth authentication was rejected (HTTP {status_code})"
    return f"Moneris OAuth request failed (HTTP {status_code})"


def _parse_json_object(response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        raise MonerisAuthError("Moneris OAuth returned an invalid response") from None
    if not isinstance(payload, dict):
        raise MonerisAuthError("Moneris OAuth returned an invalid response")
    return payload


def _require_access_token(payload: dict[str, Any]) -> str:
    if "access_token" not in payload:
        raise MonerisAuthError("Moneris OAuth returned an invalid response")
    token = payload["access_token"]
    if not isinstance(token, str) or not token.strip():
        raise MonerisAuthError("Moneris OAuth returned an invalid response")
    return token.strip()


def _require_bearer_token_type(payload: dict[str, Any]) -> None:
    if "token_type" not in payload:
        raise MonerisAuthError("Moneris OAuth returned an invalid response")
    token_type = payload["token_type"]
    if not isinstance(token_type, str) or token_type.strip().lower() != "bearer":
        raise MonerisAuthError("Moneris OAuth returned an invalid response")


def _require_expires_in(payload: dict[str, Any]) -> float:
    if "expires_in" not in payload:
        raise MonerisAuthError("Moneris OAuth returned an invalid response")
    seconds = _parse_positive_finite_number(payload["expires_in"])
    if seconds is None:
        raise MonerisAuthError("Moneris OAuth returned an invalid response")
    return seconds


def _parse_positive_finite_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            seconds = float(stripped)
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    return seconds
