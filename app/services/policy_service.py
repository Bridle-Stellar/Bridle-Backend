"""
Fast local pre-check against a cached PolicyState snapshot.

This is deliberately *not* the enforcement layer — it exists so that
obviously-doomed requests (wrong destination, over cap, kill switch on)
fail fast without spending an RPC round trip. The on-chain
check_and_record_spend call in app/services/soroban_client.py is always
the final authority; if this pre-check and the chain ever disagree, the
chain wins. Do not add any rule here that isn't also enforced on-chain.

Kept as pure functions (no I/O) so it's trivial to unit test against the
same scenarios as the contract's own test suite: over cap, over per-call
max, not allowlisted, kill switch on.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.models import RejectionReason
from app.services.soroban_client import PolicyState


@dataclass(frozen=True)
class PreCheckResult:
    approved: bool
    reason: RejectionReason | None = None
    message: str | None = None


def precheck_payment(policy: PolicyState, destination: str, amount: int) -> PreCheckResult:
    """Evaluate a single payment attempt against a policy snapshot.

    Order matters for the message a caller sees, not for correctness —
    each rule is independent, so checking in a different order would
    still catch the same violations.
    """
    if policy.kill_switch_active:
        return PreCheckResult(
            approved=False,
            reason=RejectionReason.KILL_SWITCH_ACTIVE,
            message="Spending is currently halted by the kill switch.",
        )

    if destination not in policy.allowlist:
        return PreCheckResult(
            approved=False,
            reason=RejectionReason.DESTINATION_NOT_ALLOWLISTED,
            message=f"Destination {destination} is not on the allowlist.",
        )

    if amount > policy.per_call_max:
        return PreCheckResult(
            approved=False,
            reason=RejectionReason.PER_CALL_MAX_EXCEEDED,
            message=f"Amount {amount} exceeds the per-call max of {policy.per_call_max}.",
        )

    if policy.spent_today + amount > policy.daily_cap:
        return PreCheckResult(
            approved=False,
            reason=RejectionReason.DAILY_CAP_EXCEEDED,
            message=(
                f"Amount {amount} would bring today's spend to "
                f"{policy.spent_today + amount}, exceeding the cap of {policy.daily_cap}."
            ),
        )

    return PreCheckResult(approved=True)
