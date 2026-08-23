"""Strict sandbox/production configuration for the payments API.

This module validates environment variables and fails closed on mismatch.
It does not call Moneris or Supabase, and it never rewrites incorrect URLs.

Profile IDs, client IDs, merchant IDs, and credentials are environment-specific.
This layer cannot infer whether an identifier belongs to QA or production, so
separate Vercel environment variables must be used. Never copy sandbox/QA
identifiers into production.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional
from urllib.parse import urlparse

from payment_api.errors import PaymentConfigError

SANDBOX = "sandbox"
PRODUCTION = "production"
ALLOWED_ENVIRONMENTS = frozenset({SANDBOX, PRODUCTION})

SANDBOX_API_BASE_URL = "https://api.sb.moneris.io"
PRODUCTION_API_BASE_URL = "https://api.moneris.io"

SANDBOX_HOSTED_TOKENIZATION_URL = "https://esqa.moneris.com/HPPtoken/index.php"
PRODUCTION_HOSTED_TOKENIZATION_URL = "https://www3.moneris.com/HPPtoken/index.php"

_REQUIRED_URLS = {
    SANDBOX: {
        "MONERIS_API_BASE_URL": SANDBOX_API_BASE_URL,
        "MONERIS_HOSTED_TOKENIZATION_URL": SANDBOX_HOSTED_TOKENIZATION_URL,
    },
    PRODUCTION: {
        "MONERIS_API_BASE_URL": PRODUCTION_API_BASE_URL,
        "MONERIS_HOSTED_TOKENIZATION_URL": PRODUCTION_HOSTED_TOKENIZATION_URL,
    },
}

_REDACTED = "<redacted>"

@dataclass(frozen=True)
class PaymentConfig:
    """Validated runtime configuration.

    Secret fields exist so a future Moneris/Supabase client can use them.
    Do not log this object, interpolate it into exceptions, or return it
    from HTTP handlers.
    """

    moneris_env: str
    moneris_client_id: str
    moneris_client_secret: str
    moneris_merchant_id: str
    moneris_api_base_url: str
    moneris_api_version: str
    moneris_hosted_tokenization_profile_id: str
    moneris_hosted_tokenization_url: str
    supabase_url: str
    supabase_service_role_key: str
    allowed_admin_origins: tuple[str, ...]
    allowed_public_origins: tuple[str, ...]
    enable_qa_card_validation: bool
    qa_ht_origin: Optional[str]

    def __repr__(self) -> str:
        return (
            "PaymentConfig("
            f"moneris_env={self.moneris_env!r}, "
            f"moneris_api_base_url={self.moneris_api_base_url!r}, "
            f"moneris_hosted_tokenization_url="
            f"{self.moneris_hosted_tokenization_url!r}, "
            f"moneris_api_version={self.moneris_api_version!r}, "
            f"allowed_admin_origins={self.allowed_admin_origins!r}, "
            f"allowed_public_origins={self.allowed_public_origins!r}, "
            f"enable_qa_card_validation={self.enable_qa_card_validation!r}, "
            f"qa_ht_origin={self.qa_ht_origin!r}, "
            f"moneris_client_id={_REDACTED}, "
            f"moneris_client_secret={_REDACTED}, "
            f"moneris_merchant_id={_REDACTED}, "
            f"moneris_hosted_tokenization_profile_id={_REDACTED}, "
            f"supabase_url={_REDACTED}, "
            f"supabase_service_role_key={_REDACTED})"
        )

    __str__ = __repr__


def load_config(environ: Optional[Mapping[str, str]] = None) -> PaymentConfig:
    """Load and validate configuration from ``environ`` (default: ``os.environ``).

    Incorrect sandbox/production URL combinations raise ``PaymentConfigError``.
    Values are never rewritten to "fix" a mismatch.
    """
    if environ is None:
        import os

        environ = os.environ

    moneris_env = _require_moneris_env(environ)

    moneris_client_id = _require_non_empty(environ, "MONERIS_CLIENT_ID")
    moneris_client_secret = _require_non_empty(environ, "MONERIS_CLIENT_SECRET")
    moneris_merchant_id = _require_non_empty(environ, "MONERIS_MERCHANT_ID")
    moneris_api_version = _require_non_empty(environ, "MONERIS_API_VERSION")
    moneris_hosted_tokenization_profile_id = _require_non_empty(
        environ, "MONERIS_HOSTED_TOKENIZATION_PROFILE_ID"
    )
    supabase_url = _require_non_empty(environ, "SUPABASE_URL")
    supabase_service_role_key = _require_non_empty(
        environ, "SUPABASE_SERVICE_ROLE_KEY"
    )

    expected_urls = _REQUIRED_URLS[moneris_env]
    moneris_api_base_url = _require_exact_url(
        environ,
        "MONERIS_API_BASE_URL",
        expected_urls["MONERIS_API_BASE_URL"],
        moneris_env,
    )
    moneris_hosted_tokenization_url = _require_exact_url(
        environ,
        "MONERIS_HOSTED_TOKENIZATION_URL",
        expected_urls["MONERIS_HOSTED_TOKENIZATION_URL"],
        moneris_env,
    )

    allowed_admin_origins = parse_origins(
        _optional_raw(environ, "ALLOWED_ADMIN_ORIGINS")
    )
    allowed_public_origins = parse_origins(
        _optional_raw(environ, "ALLOWED_PUBLIC_ORIGINS")
    )
    _reject_wildcard_cors_in_production(
        moneris_env, allowed_admin_origins, allowed_public_origins
    )

    enable_qa_card_validation = _flag_true(
        environ, "ENABLE_QA_CARD_VALIDATION"
    )
    qa_ht_origin = _load_qa_ht_origin(
        environ, moneris_env, enable_qa_card_validation
    )

    return PaymentConfig(
        moneris_env=moneris_env,
        moneris_client_id=moneris_client_id,
        moneris_client_secret=moneris_client_secret,
        moneris_merchant_id=moneris_merchant_id,
        moneris_api_base_url=moneris_api_base_url,
        moneris_api_version=moneris_api_version,
        moneris_hosted_tokenization_profile_id=(
            moneris_hosted_tokenization_profile_id
        ),
        moneris_hosted_tokenization_url=moneris_hosted_tokenization_url,
        supabase_url=supabase_url,
        supabase_service_role_key=supabase_service_role_key,
        allowed_admin_origins=allowed_admin_origins,
        allowed_public_origins=allowed_public_origins,
        enable_qa_card_validation=enable_qa_card_validation,
        qa_ht_origin=qa_ht_origin,
    )


def normalize_origin(raw: str) -> str:
    """Return ``scheme://host[:port]`` with no trailing slash.

    Browser ``Origin`` headers do not include a trailing slash. A configured
    value such as ``https://example.com/`` is treated as the same origin.
    """
    value = raw.strip()
    if not value or value == "*":
        raise ValueError("origin must not be empty or a wildcard")

    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("origin must be an http(s) origin")
    if parsed.netloc == "*" or parsed.hostname == "*":
        raise ValueError("origin must not be a wildcard")
    return f"{parsed.scheme}://{parsed.netloc}"


def parse_origins(raw: Optional[str]) -> tuple[str, ...]:
    """Parse a comma-separated origin list into a normalized tuple.

    Surrounding whitespace is stripped. Empty entries are ignored.
    Origins are not otherwise rewritten.
    """
    if raw is None:
        return ()

    origins: list[str] = []
    for part in raw.split(","):
        origin = part.strip()
        if origin:
            origins.append(origin)
    return tuple(origins)


def _require_moneris_env(environ: Mapping[str, str]) -> str:
    raw = _optional_raw(environ, "MONERIS_ENV")
    if raw is None:
        raise PaymentConfigError("MONERIS_ENV is required")

    value = raw.strip()
    if value not in ALLOWED_ENVIRONMENTS:
        raise PaymentConfigError(
            "MONERIS_ENV must be exactly 'sandbox' or 'production'"
        )
    return value


def _require_non_empty(environ: Mapping[str, str], name: str) -> str:
    raw = _optional_raw(environ, name)
    if raw is None:
        raise PaymentConfigError(f"{name} is required")

    value = raw.strip()
    if not value:
        raise PaymentConfigError(f"{name} must be a non-empty string")
    return value


def _require_exact_url(
    environ: Mapping[str, str],
    name: str,
    expected: str,
    moneris_env: str,
) -> str:
    actual = _require_non_empty(environ, name)
    if actual != expected:
        raise PaymentConfigError(
            f"{name} must be exactly {expected} when MONERIS_ENV is "
            f"{moneris_env}; refusing to start with a mismatched URL"
        )
    return actual


def _optional_raw(environ: Mapping[str, str], name: str) -> Optional[str]:
    if name not in environ:
        return None
    value = environ[name]
    if value is None:
        return None
    if not isinstance(value, str):
        raise PaymentConfigError(f"{name} must be a string")
    return value


def _flag_true(environ: Mapping[str, str], name: str) -> bool:
    """Return True only for an explicit true value. Absent/other -> False."""
    raw = _optional_raw(environ, name)
    if raw is None:
        return False
    return raw.strip().lower() == "true"


def _load_qa_ht_origin(
    environ: Mapping[str, str],
    moneris_env: str,
    enable_qa_card_validation: bool,
) -> Optional[str]:
    """Load the QA Hosted Tokenization origin.

    Required only when the QA bridge is enabled in sandbox. Production never
    activates the route, even if ENABLE_QA_CARD_VALIDATION=true. Wildcard
    origins are always rejected when the variable is set.
    """
    raw = _optional_raw(environ, "QA_HT_ORIGIN")
    if raw is not None:
        stripped = raw.strip()
        if stripped == "*" or (stripped and _origin_is_wildcard(stripped)):
            raise PaymentConfigError("QA_HT_ORIGIN must not be a wildcard")

    if not enable_qa_card_validation or moneris_env != SANDBOX:
        return None

    value = _require_non_empty(environ, "QA_HT_ORIGIN")
    try:
        return normalize_origin(value)
    except ValueError:
        raise PaymentConfigError(
            "QA_HT_ORIGIN must be an http(s) origin without a wildcard"
        ) from None


def _origin_is_wildcard(raw: str) -> bool:
    if raw.strip() == "*":
        return True
    try:
        parsed = urlparse(raw.strip())
    except ValueError:
        return False
    return parsed.netloc == "*" or parsed.hostname == "*"


def _reject_wildcard_cors_in_production(
    moneris_env: str,
    allowed_admin_origins: tuple[str, ...],
    allowed_public_origins: tuple[str, ...],
) -> None:
    if moneris_env != PRODUCTION:
        return
    if "*" in allowed_admin_origins or "*" in allowed_public_origins:
        raise PaymentConfigError(
            "Wildcard origin '*' is not allowed when MONERIS_ENV is production"
        )
