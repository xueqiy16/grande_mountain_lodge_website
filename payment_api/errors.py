"""Application-specific exceptions."""


class PaymentConfigError(Exception):
    """Raised when payment service configuration is missing or invalid.

    Messages must identify the problem without including secret values.
    """


class MonerisAuthError(Exception):
    """Raised when Moneris OAuth client-credentials authentication fails.

    Messages must not include access tokens, client credentials, request
    bodies, response bodies, or Authorization headers.
    """


class MonerisValidationError(Exception):
    """Raised when Moneris Card Validation fails.

    Messages must not include dataKeys, access tokens, credentials,
    request bodies, or raw Moneris response bodies.

    ``category`` is the typed business classification. Callers must branch
    on ``category``, not on exception-message substrings.
    """

    CONFIRMED_DECLINE = "CONFIRMED_DECLINE"
    INVALID_REQUEST = "INVALID_REQUEST"
    PROCESSOR_UNAVAILABLE = "PROCESSOR_UNAVAILABLE"
    INVALID_RESPONSE = "INVALID_RESPONSE"

    def __init__(self, message: str, *, category: str) -> None:
        super().__init__(message)
        if category not in {
            self.CONFIRMED_DECLINE,
            self.INVALID_REQUEST,
            self.PROCESSOR_UNAVAILABLE,
            self.INVALID_RESPONSE,
        }:
            raise ValueError("Moneris validation error category is invalid")
        self.category = category


class CredentialRegistrationError(Exception):
    """Raised when booking payment-credential registration fails.

    Messages must not include dataKeys, paymentMethodIds, issuerIds,
    OAuth tokens, Supabase secrets, or raw processor/database bodies.
    """


class CredentialConflictError(CredentialRegistrationError):
    """Raised when a registration conflicts with an existing credential."""


class CredentialPersistenceError(CredentialRegistrationError):
    """Raised when Moneris succeeded but persistence did not complete.

    Callers must retry with the same idempotency key.
    """


class CredentialReconciliationRequiredError(CredentialRegistrationError):
    """Raised after a registration is held as RECONCILIATION_REQUIRED.

    The Card Validation outcome is ambiguous or persistence after success
    did not complete. Callers must not treat this as a confirmed decline
    and must not mint a new idempotency key.
    """
