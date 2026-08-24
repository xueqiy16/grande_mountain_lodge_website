"""Server-internal booking payment-credential registration.

This is not an HTTP route. A future public/LodgeOS handler must authorize
the booking, then call this with an already-authorized ``booking_id`` and a
caller-owned ``idempotency_key``.

A logical registration request is identified by
``(registration_idempotency_key, booking_id)``. The temporary dataKey is
not part of that identity and is never persisted.

Moneris Card Validation and the Supabase write are not one ACID
transaction. Unique indexes plus guarded UPDATEs are the race authority.
Each state transition is one PostgREST statement. No RPC is used.

Same-key Create Card Validation replay was live-verified in Moneris
Sandbox for this CoF first-registration body: an identical second request
returned HTTP 201 with the same paymentMethodId and issuerId. That is
Sandbox evidence for this flow, not a universal Moneris guarantee.
``RECONCILIATION_REQUIRED`` retries must reuse the same caller-owned
idempotency key so ``validate_card`` can recover those identifiers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from payment_api.config import PaymentConfig
from payment_api.credential_repository import (
    ERROR_INVALID_RESPONSE,
    ERROR_PERSISTENCE_FAILED,
    ERROR_PROCESSOR_UNAVAILABLE,
    ERROR_VALIDATION_REJECTED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RECONCILIATION_REQUIRED,
    STATUS_SUCCEEDED,
    CredentialRecord,
    CredentialRepository,
)
from payment_api.errors import (
    CredentialConflictError,
    CredentialPersistenceError,
    CredentialRegistrationError,
    CredentialReconciliationRequiredError,
    MonerisAuthError,
    MonerisValidationError,
)
from payment_api.validation import CardValidationResult, validate_card

_SAFE_CONFLICT = "A payment credential already exists for this booking"
_SAFE_FAILED = "Card validation was rejected"
_SAFE_PERSISTENCE = (
    "Credential persistence failed; retry with the same idempotency key"
)
_SAFE_REQUEST = "Card credential registration request is invalid"


@dataclass(frozen=True)
class CredentialRegistrationResult:
    """Safe server-internal registration outcome.

    Does not include dataKey, paymentMethodId, issuerId, or raw payloads.
    """

    booking_id: UUID
    registration_status: str
    credential_stored: bool
    card_brand: Optional[str]
    last_four: Optional[str]
    expiry_month: Optional[int]
    expiry_year: Optional[int]

    def __repr__(self) -> str:
        return (
            "CredentialRegistrationResult("
            f"booking_id={self.booking_id!r}, "
            f"registration_status={self.registration_status!r}, "
            f"credential_stored={self.credential_stored!r}, "
            f"card_brand={self.card_brand!r}, "
            f"last_four={self.last_four!r}, "
            f"expiry_month={self.expiry_month!r}, "
            f"expiry_year={self.expiry_year!r})"
        )

    __str__ = __repr__


def register_booking_payment_credential(
    config: PaymentConfig,
    booking_id: object,
    data_key: object,
    idempotency_key: object,
    repository: Optional[CredentialRepository] = None,
) -> CredentialRegistrationResult:
    """Register one stored credential for an already-authorized booking.

    Does not authorize ``booking_id``. The HTTP layer must do that later.
    """
    booking = _require_uuid(booking_id, "booking")
    key = _require_uuid(idempotency_key, "idempotency")
    repo = repository or CredentialRepository(config)

    existing = repo.get_registration_by_idempotency_key(key)
    if existing is not None:
        return _resume_existing(config, data_key, key, booking, existing, repo)

    active = repo.get_active_registration_for_booking(booking)
    if active is not None:
        raise CredentialConflictError(_SAFE_CONFLICT)

    try:
        pending = repo.begin_registration(booking, key)
    except CredentialConflictError:
        raced = repo.get_registration_by_idempotency_key(key)
        if raced is not None:
            return _resume_existing(config, data_key, key, booking, raced, repo)
        raise CredentialConflictError(_SAFE_CONFLICT) from None

    return _complete_pending(config, data_key, key, pending, repo)


def _resume_existing(
    config: PaymentConfig,
    data_key: object,
    key: UUID,
    expected_booking: UUID,
    existing: CredentialRecord,
    repo: CredentialRepository,
) -> CredentialRegistrationResult:
    if existing.booking_id != expected_booking:
        raise CredentialConflictError(_SAFE_CONFLICT)
    if existing.registration_status == STATUS_SUCCEEDED:
        return _result_from_record(existing)
    if existing.registration_status == STATUS_FAILED:
        raise CredentialRegistrationError(_SAFE_FAILED)
    if existing.registration_status in (
        STATUS_PENDING,
        STATUS_RECONCILIATION_REQUIRED,
    ):
        return _complete_pending(config, data_key, key, existing, repo)
    raise CredentialRegistrationError(_SAFE_REQUEST)


def _complete_pending(
    config: PaymentConfig,
    data_key: object,
    key: UUID,
    pending: CredentialRecord,
    repo: CredentialRepository,
) -> CredentialRegistrationResult:
    try:
        validation = validate_card(config, data_key, key)
    except MonerisAuthError:
        repo.mark_processor_unresolved(
            pending.credential_id, ERROR_PROCESSOR_UNAVAILABLE
        )
        raise
    except MonerisValidationError as exc:
        return _handle_validation_error(repo, pending, exc)

    try:
        stored = repo.mark_registration_succeeded(pending.credential_id, validation)
    except CredentialConflictError:
        current = repo.get_registration_by_idempotency_key(key)
        if (
            current is not None
            and current.booking_id == pending.booking_id
            and current.registration_status == STATUS_SUCCEEDED
        ):
            return _result_from_record(current)
        raise
    except CredentialPersistenceError:
        repo.mark_reconciliation_required(
            pending.credential_id, ERROR_PERSISTENCE_FAILED
        )
        current = repo.get_registration_by_idempotency_key(key)
        if (
            current is not None
            and current.registration_status == STATUS_SUCCEEDED
        ):
            return _result_from_record(current)
        raise CredentialPersistenceError(_SAFE_PERSISTENCE) from None
    return _result_from_record(stored, validation=validation)


def _handle_validation_error(
    repo: CredentialRepository,
    pending: CredentialRecord,
    exc: MonerisValidationError,
) -> CredentialRegistrationResult:
    if exc.category == MonerisValidationError.INVALID_REQUEST:
        raise CredentialRegistrationError(_SAFE_REQUEST) from None
    if exc.category == MonerisValidationError.CONFIRMED_DECLINE:
        try:
            repo.mark_registration_failed(
                pending.credential_id, ERROR_VALIDATION_REJECTED
            )
        except CredentialPersistenceError:
            raise CredentialPersistenceError(_SAFE_PERSISTENCE) from None
        raise CredentialRegistrationError(_SAFE_FAILED) from None
    category = (
        ERROR_INVALID_RESPONSE
        if exc.category == MonerisValidationError.INVALID_RESPONSE
        else ERROR_PROCESSOR_UNAVAILABLE
    )
    repo.mark_reconciliation_required(pending.credential_id, category)
    raise CredentialReconciliationRequiredError(
        "Payment processor request failed"
    ) from None


def _result_from_record(
    record: CredentialRecord,
    validation: Optional[CardValidationResult] = None,
) -> CredentialRegistrationResult:
    return CredentialRegistrationResult(
        booking_id=record.booking_id,
        registration_status=record.registration_status,
        credential_stored=record.registration_status == STATUS_SUCCEEDED,
        card_brand=record.card_brand
        if record.card_brand is not None
        else (validation.card_brand if validation else None),
        last_four=record.last_four
        if record.last_four is not None
        else (validation.last_four if validation else None),
        expiry_month=record.expiry_month
        if record.expiry_month is not None
        else (validation.expiry_month if validation else None),
        expiry_year=record.expiry_year
        if record.expiry_year is not None
        else (validation.expiry_year if validation else None),
    )


def _require_uuid(value: object, _kind: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value.strip())
        except ValueError:
            raise CredentialRegistrationError(_SAFE_REQUEST) from None
    raise CredentialRegistrationError(_SAFE_REQUEST)
