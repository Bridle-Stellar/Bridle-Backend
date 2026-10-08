"""
The ScVal decoders in app/services/soroban_client.py, run against real
values read back from Bridle-Contract's testnet deployment
(tests/fixtures/testnet_interface.py), plus the malformed shapes they must
refuse rather than paper over with defaults.
"""
import pytest
from stellar_sdk import Address, scval
from stellar_sdk import xdr as stellar_xdr

from app.services.soroban_client import (
    CONTRACT_REJECT_REASONS,
    SorobanCallError,
    _parse_policy_snapshot,
    _parse_spend_outcome,
    _parse_spend_status,
    decode_raw_event,
    parse_spend_outcome_value,
)
from tests.fixtures import testnet_interface as real


def _scval(b64: str) -> stellar_xdr.SCVal:
    return stellar_xdr.SCVal.from_xdr(b64)


def _map(**fields) -> stellar_xdr.SCVal:
    # soroban-sdk sorts struct map keys; do the same so these look like real contract output.
    return scval.to_map({scval.to_symbol(k): v for k, v in sorted(fields.items())})


def _valid_policy_fields(**overrides) -> dict:
    fields = dict(
        agents=scval.to_vec([scval.to_address(real.AGENT)]),
        allowlist=scval.to_vec([_map(category=scval.to_symbol("compute"), destination=scval.to_address(real.MERCHANT))]),
        daily_cap=scval.to_int128(10_000_000_000),
        kill_switch=scval.to_bool(False),
        owner=scval.to_address(real.OWNER),
        per_call_max=scval.to_int128(3_000_000_000),
        token=scval.to_address(real.TOKEN),
    )
    fields.update(overrides)
    return fields


# -- get_policy -------------------------------------------------------------------


def test_policy_snapshot_from_testnet():
    snapshot = _parse_policy_snapshot(_scval(real.GET_POLICY_RESULT))

    assert snapshot.owner == real.OWNER
    assert snapshot.agents == [real.AGENT]
    assert snapshot.token == real.TOKEN
    assert snapshot.daily_cap == 10_000_000_000
    assert snapshot.per_call_max == 3_000_000_000
    assert snapshot.kill_switch_active is False
    assert len(snapshot.allowlist) == 1
    assert snapshot.allowlist[0].destination == real.MERCHANT
    assert snapshot.allowlist[0].category == "compute"


def test_policy_snapshot_reads_kill_switch_on():
    snapshot = _parse_policy_snapshot(_map(**_valid_policy_fields(kill_switch=scval.to_bool(True))))
    assert snapshot.kill_switch_active is True


def test_policy_snapshot_missing_kill_switch_raises_instead_of_defaulting_off():
    fields = _valid_policy_fields()
    del fields["kill_switch"]
    with pytest.raises(SorobanCallError, match="kill_switch"):
        _parse_policy_snapshot(_map(**fields))


def test_policy_snapshot_rejects_old_guessed_field_name():
    # The pre-verification parser read `kill_switch_active`; the contract calls it `kill_switch`.
    fields = _valid_policy_fields()
    fields["kill_switch_active"] = fields.pop("kill_switch")
    with pytest.raises(SorobanCallError):
        _parse_policy_snapshot(_map(**fields))


def test_policy_snapshot_wrong_type_raises():
    with pytest.raises(SorobanCallError, match="daily_cap"):
        _parse_policy_snapshot(_map(**_valid_policy_fields(daily_cap=scval.to_bool(True))))


def test_policy_snapshot_non_map_raises():
    with pytest.raises(SorobanCallError):
        _parse_policy_snapshot(scval.to_void())


# -- get_spend_status ------------------------------------------------------------------


def test_spend_status_from_testnet():
    status = _parse_spend_status(_scval(real.GET_SPEND_STATUS_RESULT))

    assert status.daily_cap == 10_000_000_000
    assert status.period_start == 1791417600
    assert status.spent_today == 500_000_000
    assert status.remaining_today == 9_500_000_000


def test_spend_status_negative_remaining_is_kept_signed():
    status = _parse_spend_status(
        _map(
            daily_cap=scval.to_int128(100),
            period_start=scval.to_uint64(1791417600),
            remaining_today=scval.to_int128(-50),
            spent_today=scval.to_int128(150),
        )
    )
    assert status.remaining_today == -50


def test_spend_status_missing_field_raises():
    with pytest.raises(SorobanCallError, match="spent_today"):
        _parse_spend_status(_map(daily_cap=scval.to_int128(1), period_start=scval.to_uint64(1), remaining_today=scval.to_int128(1)))


# -- check_and_record_spend → SpendOutcome -----------------------------------------------


def test_spend_outcome_approved_from_testnet_meta_v4():
    outcome = _parse_spend_outcome(real.META_SPEND_APPROVED)

    assert outcome.approved is True
    assert outcome.spent_today == 500_000_000
    assert outcome.remaining_today == 9_500_000_000


def test_spend_outcome_rejected_from_testnet_meta_v4():
    outcome = _parse_spend_outcome(real.META_SPEND_REJECTED)

    assert outcome.approved is False
    assert outcome.reason == "ExceedsPerCallMax"


@pytest.mark.parametrize("variant", CONTRACT_REJECT_REASONS)
def test_spend_outcome_every_reject_reason(variant):
    value = scval.to_vec([scval.to_symbol("Rejected"), scval.to_vec([scval.to_symbol(variant)])])
    assert parse_spend_outcome_value(value).reason == variant


@pytest.mark.parametrize(
    "value",
    [
        scval.to_vec([scval.to_symbol("Approved")]),
        scval.to_vec([scval.to_symbol("Rejected"), scval.to_symbol("ExceedsPerCallMax")]),  # bare symbol, not the enum vec
        scval.to_vec([scval.to_symbol("Rejected"), scval.to_vec([scval.to_symbol("SomethingNew")])]),
        scval.to_vec([scval.to_symbol("Pending"), scval.to_void()]),
        scval.to_map({scval.to_symbol("Approved"): scval.to_void()}),
    ],
)
def test_spend_outcome_unrecognized_shapes_raise(value):
    with pytest.raises(SorobanCallError):
        parse_spend_outcome_value(value)


def test_spend_outcome_without_meta_raises():
    with pytest.raises(SorobanCallError):
        _parse_spend_outcome(None)


# -- events ------------------------------------------------------------------------------


def test_spend_approved_event_from_testnet():
    event = decode_raw_event(*real.EVENT_SPEND_APPROVED)

    assert event.event_type == "spend_approved"
    assert event.topic == f"spend_approved.{real.AGENT}.{real.MERCHANT}"
    assert event.data == {"amount": 500_000_000, "remaining_today": 9_500_000_000, "spent_today": 500_000_000, "token": real.TOKEN}


def test_spend_rejected_event_from_testnet():
    event = decode_raw_event(*real.EVENT_SPEND_REJECTED_PER_CALL)

    assert event.event_type == "spend_rejected"
    assert event.data == {"amount": 4_000_000_000, "reason": ["ExceedsPerCallMax"], "token": real.TOKEN}


def test_policy_events_from_testnet():
    added = decode_raw_event(*real.EVENT_ALLOWLIST_ENTRY_ADDED)
    assert added.topic == f"allowlist_entry_added.{real.MERCHANT}"
    assert added.data == {"category": "compute"}

    toggled = decode_raw_event(*real.EVENT_KILL_SWITCH_ON)
    assert toggled.topic == "kill_switch_toggled"
    assert toggled.data == {"active": True}


def test_event_data_holds_no_address_objects():
    # json.dumps in the sync worker would raise on a stellar_sdk Address.
    import json

    for raw in (real.EVENT_SPEND_APPROVED, real.EVENT_SPEND_REJECTED_PER_CALL, real.EVENT_ALLOWLIST_ENTRY_ADDED):
        event = decode_raw_event(*raw)
        json.dumps(event.data)
        assert "Address" not in event.topic
        assert not any(isinstance(v, Address) for v in event.data.values())
