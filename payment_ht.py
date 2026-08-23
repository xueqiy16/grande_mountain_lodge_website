"""Server-side Hosted Tokenization config for the complete-payment page.

Browser receives only the HT iframe URL and its exact postMessage origin.
MONERIS_CLIENT_SECRET, client ID, merchant ID, API base URL, and Supabase
keys are never included. URLs are never rewritten to "fix" a mismatch.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional
from urllib.parse import quote, urlencode, urlparse

from payment_api.config import (
    PRODUCTION_HOSTED_TOKENIZATION_URL,
    SANDBOX_HOSTED_TOKENIZATION_URL,
)
from payment_api.validation import (
    TEMPORARY_TOKEN_MAX_LENGTH,
    TEMPORARY_TOKEN_MIN_LENGTH,
)

# Same required iframe CSS/query as the verified QA harness.
IFRAME_CSS_BODY = "background:#ffffff;margin:0;padding:8px;color:#222222;"
IFRAME_CSS_TEXTBOX = (
    "font-size:16px;border:1px solid #cccccc;margin:0 0 8px 0;"
    "padding:6px;height:32px;width:220px;"
)

_REQUIRED_HT_URLS = {
    "sandbox": SANDBOX_HOSTED_TOKENIZATION_URL,
    "production": PRODUCTION_HOSTED_TOKENIZATION_URL,
}


class HostedTokenizationConfigError(Exception):
    """Raised when HT browser config is missing or mismatched.

    Messages must not include the Profile ID or other secrets.
    """


@dataclass(frozen=True)
class HostedTokenizationBrowserConfig:
    hosted_tokenization_url: str
    iframe_src: str
    postmessage_origin: str
    token_min_length: int
    token_max_length: int

    def __repr__(self) -> str:
        return (
            "HostedTokenizationBrowserConfig("
            f"hosted_tokenization_url={self.hosted_tokenization_url!r}, "
            f"postmessage_origin={self.postmessage_origin!r}, "
            f"token_min_length={self.token_min_length!r}, "
            f"token_max_length={self.token_max_length!r}, "
            "iframe_src=<redacted>)"
        )

    __str__ = __repr__


def load_hosted_tokenization_browser_config(
    environ: Optional[Mapping[str, str]] = None,
) -> HostedTokenizationBrowserConfig:
    """Load non-secret HT values after exact sandbox/production URL checks."""
    source = os.environ if environ is None else environ
    moneris_env = _require_non_empty(source, "MONERIS_ENV")
    if moneris_env not in _REQUIRED_HT_URLS:
        raise HostedTokenizationConfigError(
            "MONERIS_ENV must be exactly 'sandbox' or 'production'"
        )
    expected_url = _REQUIRED_HT_URLS[moneris_env]
    hosted_tokenization_url = _require_non_empty(
        source, "MONERIS_HOSTED_TOKENIZATION_URL"
    )
    if hosted_tokenization_url != expected_url:
        raise HostedTokenizationConfigError(
            "MONERIS_HOSTED_TOKENIZATION_URL must be exactly "
            f"{expected_url} when MONERIS_ENV is {moneris_env}; "
            "refusing to start with a mismatched URL"
        )
    profile_id = _require_non_empty(
        source, "MONERIS_HOSTED_TOKENIZATION_PROFILE_ID"
    )
    origin = _origin_from_ht_url(hosted_tokenization_url)
    return HostedTokenizationBrowserConfig(
        hosted_tokenization_url=hosted_tokenization_url,
        iframe_src=build_iframe_src(hosted_tokenization_url, profile_id),
        postmessage_origin=origin,
        token_min_length=TEMPORARY_TOKEN_MIN_LENGTH,
        token_max_length=TEMPORARY_TOKEN_MAX_LENGTH,
    )


def build_iframe_src(hosted_tokenization_url: str, profile_id: str) -> str:
    """Build the Moneris iframe URL with the verified QA query parameters."""
    query = urlencode(
        {
            "id": profile_id,
            "pmmsg": "true",
            "css_body": IFRAME_CSS_BODY,
            "css_textbox": IFRAME_CSS_TEXTBOX,
            "enable_exp": "1",
            "enable_cvd": "1",
            "display_labels": "1",
        },
        quote_via=quote,
    )
    return f"{hosted_tokenization_url}?{query}"


def _origin_from_ht_url(hosted_tokenization_url: str) -> str:
    parsed = urlparse(hosted_tokenization_url)
    if parsed.scheme not in {"https"} or not parsed.netloc:
        raise HostedTokenizationConfigError(
            "MONERIS_HOSTED_TOKENIZATION_URL must be an https origin"
        )
    return f"{parsed.scheme}://{parsed.netloc}"


def _require_non_empty(environ: Mapping[str, str], name: str) -> str:
    raw = environ.get(name)
    if raw is None or not str(raw).strip():
        raise HostedTokenizationConfigError(f"{name} is required")
    return str(raw).strip()
