"""
Sync worker ingestion, fed with real spend events from Bridle-Contract's
testnet deployment (tests/fixtures/testnet_interface.py) decoded through
the same path fetch_new_events uses.
"""
from sqlalchemy import select

from app.models import ContractEvent, PaymentStatus, RejectionReason, Transaction
from app.services.soroban_client import CONTRACT_REJECT_REASONS, decode_raw_event
from app.services.sync_worker import CONTRACT_REASON_TO_REJECTION, _ingest_event
from tests.fixtures import testnet_interface as real


async def _ingest(session_factory, *raw_events):
    async with session_factory() as db:
        for raw in raw_events:
            await _ingest_event(db, decode_raw_event(*raw))
        await db.commit()
    async with session_factory() as db:
        txs = (await db.execute(select(Transaction).order_by(Transaction.contract_tx_hash))).scalars().all()
        events = (await db.execute(select(ContractEvent))).scalars().all()
    return txs, events


async def test_backfills_approved_spend(session_factory):
    txs, events = await _ingest(session_factory, real.EVENT_SPEND_APPROVED)

    assert len(events) == 1
    [tx] = txs
    assert tx.status == PaymentStatus.APPROVED
    assert tx.agent == real.AGENT
    assert tx.destination == real.MERCHANT
    assert tx.token == real.TOKEN
    assert tx.amount == 500_000_000
    assert tx.rejection_reason is None
    assert tx.contract_tx_hash == real.EVENT_SPEND_APPROVED[1]
    assert tx.source == "sync"


async def test_backfills_rejected_spends_with_mapped_reason(session_factory):
    txs, _ = await _ingest(session_factory, real.EVENT_SPEND_REJECTED_PER_CALL, real.EVENT_SPEND_REJECTED_KILL_SWITCH)

    by_hash = {tx.contract_tx_hash: tx for tx in txs}
    per_call = by_hash[real.EVENT_SPEND_REJECTED_PER_CALL[1]]
    killed = by_hash[real.EVENT_SPEND_REJECTED_KILL_SWITCH[1]]

    assert per_call.status == PaymentStatus.REJECTED
    assert per_call.rejection_reason == RejectionReason.PER_CALL_MAX_EXCEEDED
    assert "ExceedsPerCallMax" in per_call.detail
    assert killed.rejection_reason == RejectionReason.KILL_SWITCH_ACTIVE


async def test_policy_events_are_logged_but_not_transactions(session_factory):
    txs, events = await _ingest(session_factory, real.EVENT_ALLOWLIST_ENTRY_ADDED, real.EVENT_KILL_SWITCH_ON)

    assert txs == []
    assert sorted(e.event_type for e in events) == ["allowlist_entry_added", "kill_switch_toggled"]


async def test_reingesting_same_spend_does_not_duplicate(session_factory):
    await _ingest(session_factory, real.EVENT_SPEND_APPROVED)
    txs, _ = await _ingest(session_factory, real.EVENT_SPEND_APPROVED)
    assert len(txs) == 1


def test_every_contract_reject_reason_is_mapped():
    assert set(CONTRACT_REASON_TO_REJECTION) == set(CONTRACT_REJECT_REASONS)
