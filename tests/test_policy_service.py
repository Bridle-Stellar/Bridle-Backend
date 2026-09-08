"""
Unit tests for the local pre-check logic. Mirrors the scenarios expected
of the contract's own test suite so the two layers can't silently drift
apart: over cap, over per-call max, not allowlisted, kill switch on.
"""
from app.models import RejectionReason
from app.services.policy_service import precheck_payment
from app.services.soroban_client import PolicyState

DEST = "GDEST0000000000000000000000000000000000000000000000000"
OTHER = "GOTHER0000000000000000000000000000000000000000000000000"


def make_policy(**overrides) -> PolicyState:
    base = dict(
        allowlist=[DEST],
        per_call_max=5_000_000,
        daily_cap=10_000_000,
        spent_today=0,
        kill_switch_active=False,
        token="native",
    )
    base.update(overrides)
    return PolicyState(**base)


def test_approves_a_valid_payment():
    result = precheck_payment(make_policy(), DEST, 1_000_000)
    assert result.approved
    assert result.reason is None


def test_rejects_when_kill_switch_active():
    result = precheck_payment(make_policy(kill_switch_active=True), DEST, 1_000_000)
    assert not result.approved
    assert result.reason == RejectionReason.KILL_SWITCH_ACTIVE


def test_kill_switch_takes_priority_over_other_violations():
    # Even a destination that's also not allowlisted should report the
    # kill switch first — it's the coarsest-grained rule.
    policy = make_policy(kill_switch_active=True)
    result = precheck_payment(policy, OTHER, 999_999_999)
    assert result.reason == RejectionReason.KILL_SWITCH_ACTIVE


def test_rejects_destination_not_allowlisted():
    result = precheck_payment(make_policy(), OTHER, 1_000_000)
    assert not result.approved
    assert result.reason == RejectionReason.DESTINATION_NOT_ALLOWLISTED


def test_rejects_over_per_call_max():
    result = precheck_payment(make_policy(per_call_max=1_000_000), DEST, 1_000_001)
    assert not result.approved
    assert result.reason == RejectionReason.PER_CALL_MAX_EXCEEDED


def test_allows_exactly_the_per_call_max():
    result = precheck_payment(make_policy(per_call_max=1_000_000), DEST, 1_000_000)
    assert result.approved


def test_rejects_over_daily_cap():
    policy = make_policy(spent_today=9_000_000, daily_cap=10_000_000)
    result = precheck_payment(policy, DEST, 2_000_000)
    assert not result.approved
    assert result.reason == RejectionReason.DAILY_CAP_EXCEEDED


def test_allows_spend_that_exactly_reaches_the_cap():
    policy = make_policy(spent_today=9_000_000, daily_cap=10_000_000)
    result = precheck_payment(policy, DEST, 1_000_000)
    assert result.approved
