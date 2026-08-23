"""Server-side PostgREST access for booking payment credentials.

Uses the existing ``requests`` dependency against Supabase REST. There is
no ORM. The backend ``SUPABASE_SERVICE_ROLE_KEY`` (legacy service_role JWT,
or a future ``sb_secret_`` key stored in that variable) stays server-side
and bypasses RLS.

Never log row bodies, paymentMethodIds, issuerIds, or the backend key.
This module does not write logs; exception messages are public-safe.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

import requests

from payment_api.auth import OAUTH_TIMEOUT
from payment_api.config import PaymentConfig
from payment_api.errors import (
    CredentialConflictError,
    CredentialPersistenceError,
)
from payment_api.validation import CardValidationResult

TABLE_PATH = "/rest/v1/booking_payment_credentials"
STATUS_PENDING = "PENDING"
STATUS_SUCCEEDED = "SUCCEEDED"
STATUS_FAILED = "FAILED"
STATUS_RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
ACTIVE_REGISTRATION_STATUSES = (
    STATUS_PENDING,
    STATUS_SUCCEEDED,
    STATUS_RECONCILIATION_REQUIRED,
)
WRITABLE_REGISTRATION_STATUSES = (
    STATUS_PENDING,
    STATUS_RECONCILIATION_REQUIRED,
)
ERROR_VALIDATION_REJECTED = "VALIDATION_REJECTED"
ERROR_PROCESSOR_UNAVAILABLE = "PROCESSOR_UNAVAILABLE"
ERROR_INVALID_RESPONSE = "INVALID_RESPONSE"
ERROR_PERSISTENCE_FAILED = "PERSISTENCE_FAILED"
_WRITABLE_STATUS_FILTER = "in.(PENDING,RECONCILIATION_REQUIRED)"
_ACTIVE_STATUS_FILTER = "in.(PENDING,SUCCEEDED,RECONCILIATION_REQUIRED)"
_SAFE_CONFLICT = "A payment credential already exists for this booking"
_SAFE_STALE = "Credential registration state changed"
_SAFE_PERSISTENCE = (
    "Credential persistence failed; retry with the same idempotency key"
)
_REDACTED = "<redacted>"


@dataclass(frozen=True)
class CredentialRecord:
    """One booking_payment_credentials row.

    Identifiers are present for server-internal use and are redacted in
    ``repr``.
    """

    credential_id: UUID
    booking_id: UUID
    registration_idempotency_key: UUID
    registration_status: str
    registration_error_category: Optional[str]
    moneris_payment_method_id: Optional[str]
    moneris_issuer_id: Optional[str]
    card_brand: Optional[str]
    last_four: Optional[str]
    expiry_month: Optional[int]
    expiry_year: Optional[int]
    registered_at: Optional[str]

    def __repr__(self) -> str:
        return (
            "CredentialRecord("
            f"credential_id={self.credential_id!r}, "
            f"booking_id={self.booking_id!r}, "
            f"registration_idempotency_key={self.registration_idempotency_key!r}, "
            f"registration_status={self.registration_status!r}, "
            f"registration_error_category={self.registration_error_category!r}, "
            f"moneris_payment_method_id={_REDACTED}, "
            f"moneris_issuer_id={_REDACTED}, "
            f"card_brand={self.card_brand!r}, "
            f"last_four={self.last_four!r}, "
            f"expiry_month={self.expiry_month!r}, "
            f"expiry_year={self.expiry_year!r})"
        )

    __str__ = __repr__


class CredentialRepository:
    """Narrow PostgREST client for credential registration."""

    def __init__(self, config: PaymentConfig) -> None:
        self._config = config

    def get_registration_by_idempotency_key(
        self, idempotency_key: UUID
    ) -> Optional[CredentialRecord]:
        rows = self._select(
            {"registration_idempotency_key": f"eq.{idempotency_key}"}
        )
        if not rows:
            return None
        return _row_to_record(rows[0])

    def get_active_registration_for_booking(
        self, booking_id: UUID
    ) -> Optional[CredentialRecord]:
        rows = self._select(
            {
                "booking_id": f"eq.{booking_id}",
                "registration_status": _ACTIVE_STATUS_FILTER,
            }
        )
        if not rows:
            return None
        return _row_to_record(rows[0])

    def begin_registration(
        self, booking_id: UUID, idempotency_key: UUID
    ) -> CredentialRecord:
        """Insert PENDING, or return canonical state after a uniqueness race.

        Unique indexes are the authority. A 409 is re-read, never retried
        with a new key.
        """
        payload = {
            "booking_id": str(booking_id),
            "registration_idempotency_key": str(idempotency_key),
            "registration_status": STATUS_PENDING,
        }
        try:
            rows = self._request(
                "POST",
                "",
                json_body=payload,
                extra_headers={"Prefer": "return=representation"},
            )
        except CredentialConflictError:
            return self._resolve_insert_conflict(booking_id, idempotency_key)
        if not rows:
            raise CredentialPersistenceError(_SAFE_PERSISTENCE)
        return _row_to_record(rows[0])

    def mark_registration_succeeded(
        self, credential_id: UUID, result: CardValidationResult
    ) -> CredentialRecord:
        now = datetime.now(timezone.utc).isoformat()
        payload = {
            "registration_status": STATUS_SUCCEEDED,
            "registration_error_category": None,
            "moneris_payment_method_id": result.payment_method_id,
            "moneris_issuer_id": result.issuer_id,
            "card_brand": result.card_brand,
            "last_four": result.last_four,
            "expiry_month": result.expiry_month,
            "expiry_year": result.expiry_year,
            "registered_at": now,
            "updated_at": now,
        }
        return self._guarded_patch(credential_id, payload)

    def mark_registration_failed(
        self, credential_id: UUID, error_category: str
    ) -> CredentialRecord:
        now = datetime.now(timezone.utc).isoformat()
        payload = {
            "registration_status": STATUS_FAILED,
            "registration_error_category": error_category,
            "updated_at": now,
        }
        return self._guarded_patch(credential_id, payload)

    def mark_processor_unresolved(
        self, credential_id: UUID, error_category: str
    ) -> Optional[CredentialRecord]:
        """Keep PENDING when Card Validation was never sent (e.g. OAuth)."""
        now = datetime.now(timezone.utc).isoformat()
        payload = {
            "registration_error_category": error_category,
            "updated_at": now,
        }
        try:
            return self._guarded_patch(
                credential_id,
                payload,
                status_filter="eq.PENDING",
            )
        except (CredentialConflictError, CredentialPersistenceError):
            return None

    def mark_reconciliation_required(
        self, credential_id: UUID, error_category: str
    ) -> Optional[CredentialRecord]:
        """Block a new idempotency key until the same key is reconciled."""
        now = datetime.now(timezone.utc).isoformat()
        payload = {
            "registration_status": STATUS_RECONCILIATION_REQUIRED,
            "registration_error_category": error_category,
            "updated_at": now,
        }
        try:
            return self._guarded_patch(credential_id, payload)
        except CredentialConflictError:
            return None
        except CredentialPersistenceError:
            return None

    def _resolve_insert_conflict(
        self, booking_id: UUID, idempotency_key: UUID
    ) -> CredentialRecord:
        existing = self.get_registration_by_idempotency_key(idempotency_key)
        if existing is not None:
            if existing.booking_id != booking_id:
                raise CredentialConflictError(_SAFE_CONFLICT)
            return existing
        active = self.get_active_registration_for_booking(booking_id)
        if active is not None:
            raise CredentialConflictError(_SAFE_CONFLICT)
        raise CredentialPersistenceError(_SAFE_PERSISTENCE)

    def _guarded_patch(
        self,
        credential_id: UUID,
        payload: dict[str, Any],
        status_filter: str = _WRITABLE_STATUS_FILTER,
    ) -> CredentialRecord:
        query = (
            f"?credential_id=eq.{credential_id}"
            f"&registration_status={status_filter}"
        )
        rows = self._request(
            "PATCH",
            query,
            json_body=payload,
            extra_headers={"Prefer": "return=representation"},
        )
        if not rows:
            raise CredentialConflictError(_SAFE_STALE)
        return _row_to_record(rows[0])

    def _select(self, filters: dict[str, str]) -> list[dict[str, Any]]:
        query = "&".join(f"{key}={value}" for key, value in filters.items())
        rows = self._request("GET", f"?{query}&select=*")
        return rows

    def _headers(self) -> dict[str, str]:
        key = self._config.supabase_service_role_key
        return {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        query: str,
        json_body: Optional[dict[str, Any]] = None,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> list[dict[str, Any]]:
        url = f"{self._config.supabase_url.rstrip('/')}{TABLE_PATH}{query}"
        headers = self._headers()
        if extra_headers:
            headers.update(extra_headers)
        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                json=json_body,
                timeout=OAUTH_TIMEOUT,
            )
        except requests.RequestException:
            raise CredentialPersistenceError(_SAFE_PERSISTENCE) from None

        if response.status_code == 409:
            raise CredentialConflictError(_SAFE_CONFLICT)
        if not (200 <= response.status_code < 300):
            raise CredentialPersistenceError(_SAFE_PERSISTENCE)
        if response.status_code == 204 or not response.content:
            return []
        try:
            payload = response.json()
        except ValueError:
            raise CredentialPersistenceError(_SAFE_PERSISTENCE) from None
        if payload is None:
            return []
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            return [payload]
        raise CredentialPersistenceError(_SAFE_PERSISTENCE)


def _row_to_record(row: dict[str, Any]) -> CredentialRecord:
    try:
        return CredentialRecord(
            credential_id=UUID(str(row["credential_id"])),
            booking_id=UUID(str(row["booking_id"])),
            registration_idempotency_key=UUID(
                str(row["registration_idempotency_key"])
            ),
            registration_status=str(row["registration_status"]),
            registration_error_category=_optional_str(
                row.get("registration_error_category")
            ),
            moneris_payment_method_id=_optional_str(
                row.get("moneris_payment_method_id")
            ),
            moneris_issuer_id=_optional_str(row.get("moneris_issuer_id")),
            card_brand=_optional_str(row.get("card_brand")),
            last_four=_optional_str(row.get("last_four")),
            expiry_month=_optional_int(row.get("expiry_month")),
            expiry_year=_optional_int(row.get("expiry_year")),
            registered_at=_optional_str(row.get("registered_at")),
        )
    except (KeyError, TypeError, ValueError):
        raise CredentialPersistenceError(_SAFE_PERSISTENCE) from None


def _optional_str(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _optional_int(value: object) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value
