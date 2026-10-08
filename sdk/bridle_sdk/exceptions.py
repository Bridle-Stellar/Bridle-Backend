"""Exceptions surfaced by BridleClient, mirroring the relay's structured rejections."""


class BridleRejected(Exception):
    """Raised when the Bridle relay denies a payment.

    Attributes mirror the backend's structured rejection body so callers
    can branch on `reason` (e.g. "daily_cap_exceeded") without parsing
    free-text messages.
    """

    def __init__(self, reason: str, message: str, transaction_id: str | None = None):
        self.reason = reason
        self.message = message
        self.transaction_id = transaction_id
        super().__init__(f"{reason}: {message}")


class BridleUpstreamError(Exception):
    """Raised for anything that isn't a policy rejection: network errors,
    unexpected relay responses, or the relay's own 5xx errors."""


class BridleVerificationError(BridleUpstreamError):
    """Raised before signing when the relay's /relay/prepare response asks
    the agent to authorize something other than the payment it requested
    (different destination, amount, token, contract, network, or extra
    nested authorizations). Nothing was signed or sent. Treat the relay as
    untrustworthy until you know why."""
