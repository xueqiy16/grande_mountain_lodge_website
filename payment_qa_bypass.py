"""TEMPORARY QA-only pause bypass for one sandbox booking.

Delete this module, its tests, the /internal/payment-qa-auth route, and
PAYMENT_QA_BYPASS_SECRET after the controlled test. Do not leave this
wired once public bookings are restored.

DIRECT_BOOKINGS_PAUSED stays True. This never skips payment, never
grants admin/cron access, and never changes pending_v7 semantics.

Activation is a same-origin POST that sets a short-lived HttpOnly cookie.
The secret must never appear in query strings, HTML, JS, JSON, or logs.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from typing import Optional

from flask import current_app, jsonify, make_response, redirect, request

QA_SECRET_ENV = "PAYMENT_QA_BYPASS_SECRET"
MIN_QA_SECRET_LENGTH = 32
TTL_SECONDS = 30 * 60
COOKIE_NAME = "gml_qa_booking"
COOKIE_VERSION = "v1"
QA_AUTH_PATH = "/internal/payment-qa-auth"
BOOKING_LANDING = "/booking"

SAFE_UNAUTHORIZED = "Unauthorized."

_HMAC_CONTEXT = "gml-payment-qa-v1"

# Unlisted native form. No secret, no JS, not linked from public pages.
_QA_FORM_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="robots" content="noindex,nofollow">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Authorization</title>
</head>
<body>
<form method="post" action="/internal/payment-qa-auth" autocomplete="off">
<label>Authorization
<input type="password" name="secret" required minlength="32" autocomplete="off">
</label>
<button type="submit">Continue</button>
</form>
</body>
</html>
"""


def configured_qa_secret() -> Optional[str]:
    raw = os.getenv(QA_SECRET_ENV)
    if raw is None:
        return None
    secret = str(raw).strip()
    if len(secret) < MIN_QA_SECRET_LENGTH:
        return None
    return secret


def authorized(req=None) -> bool:
    """True only with a valid, unexpired QA booking cookie."""
    expected = configured_qa_secret()
    if expected is None:
        return False
    incoming = req if req is not None else request
    raw = incoming.cookies.get(COOKIE_NAME)
    if not isinstance(raw, str) or not raw:
        return False
    return _cookie_valid(raw, expected)


def handle_qa_auth_request():
    """GET serves an unlisted password form; POST mints the QA cookie."""
    if request.method == "GET":
        return _handle_get()
    return _handle_post()


def mint_cookie_value(secret: str, *, now: Optional[int] = None) -> str:
    exp = int(now if now is not None else _now()) + TTL_SECONDS
    return f"{COOKIE_VERSION}.{exp}.{_mac(secret, exp)}"


def apply_qa_cookie(response, secret: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        mint_cookie_value(secret),
        max_age=TTL_SECONDS,
        httponly=True,
        secure=_cookie_secure(),
        samesite="Lax",
        path="/",
    )


def _handle_get():
    if configured_qa_secret() is None:
        return _empty_404()
    response = make_response(_QA_FORM_HTML, 200)
    response.headers["Content-Type"] = "text/html; charset=utf-8"
    _lock_down_headers(response)
    return response


def _handle_post():
    expected = configured_qa_secret()
    provided = _secret_from_body().strip()
    if expected is None or not _secrets_match(provided, expected):
        return _unauthorized_response()
    if _wants_json():
        response = jsonify({"ok": True})
        apply_qa_cookie(response, expected)
        _lock_down_headers(response)
        return response, 200
    response = redirect(BOOKING_LANDING)
    apply_qa_cookie(response, expected)
    _lock_down_headers(response)
    return response


def _secret_from_body() -> str:
    """Read the secret from JSON or form body only. Ignore query strings."""
    if request.is_json:
        payload = request.get_json(silent=True)
        if isinstance(payload, dict):
            value = payload.get("secret")
            if isinstance(value, str):
                return value
            return ""
    value = request.form.get("secret")
    if isinstance(value, str):
        return value
    return ""


def _wants_json() -> bool:
    if request.is_json:
        return True
    accept = request.accept_mimetypes
    return accept["application/json"] > accept["text/html"]


def _unauthorized_response():
    if _wants_json():
        response = jsonify({"ok": False, "error": SAFE_UNAUTHORIZED})
        _lock_down_headers(response)
        return response, 401
    response = make_response(SAFE_UNAUTHORIZED, 401)
    response.headers["Content-Type"] = "text/plain; charset=utf-8"
    _lock_down_headers(response)
    return response


def _empty_404():
    response = make_response("", 404)
    _lock_down_headers(response)
    return response


def _cookie_valid(value: str, secret: str) -> bool:
    parts = value.split(".")
    if len(parts) != 3:
        return False
    version, exp_s, mac = parts
    if version != COOKIE_VERSION:
        return False
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    now = _now()
    if exp <= now or exp > now + TTL_SECONDS + 60:
        return False
    return _secrets_match(mac, _mac(secret, exp))


def _mac(secret: str, exp: int) -> str:
    message = f"{_HMAC_CONTEXT}|{exp}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _secrets_match(provided: str, expected: str) -> bool:
    if not isinstance(provided, str) or not isinstance(expected, str):
        return False
    try:
        return secrets.compare_digest(provided, expected)
    except (TypeError, ValueError):
        return False


def _cookie_secure() -> bool:
    try:
        return not bool(current_app.config.get("TESTING"))
    except RuntimeError:
        return True


def _now() -> int:
    return int(time.time())


def _lock_down_headers(response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Referrer-Policy"] = "no-referrer"
