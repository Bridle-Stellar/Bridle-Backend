"""
Background polling worker that keeps the local transaction log accurate
even when a spend happens on-chain without going through /relay (e.g. a
developer calling check_and_record_spend directly via CLI while testing).

v1 is a plain polling loop, not an event-streaming pipeline, per the
repo's stated non-goals — see README "Tuning the sync worker" for how to
adjust the interval.
"""
from __future__ import annotations

import asyncio
import json
import logging

from sqlalchemy import select

from app.config import Settings
from app.database import session_scope
from app.models import ContractEvent, PaymentStatus, RejectionReason, SyncCursor, Transaction
from app.services.soroban_client import (
    EVENT_SPEND_APPROVED,
    EVENT_SPEND_REJECTED,
    RawContractEvent,
    SorobanContractClient,
)

logger = logging.getLogger("bridle.sync_worker")


class SyncWorker:
    def __init__(self, client: SorobanContractClient, settings: Settings):
        self._client = client
        self._settings = settings
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run_loop(), name="bridle-sync-worker")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            await self._task

    async def _run_loop(self) -> None:
        logger.info("Sync worker starting (poll interval=%.1fs)", self._settings.sync_poll_interval_seconds)
        while not self._stop_event.is_set():
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 - a single bad poll must not kill the worker
                logger.exception("Sync worker iteration failed; will retry next interval")

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._settings.sync_poll_interval_seconds)
            except asyncio.TimeoutError:
                pass

    async def run_once(self) -> int:
        """Runs a single poll iteration. Returns the number of events ingested.
        Exposed separately from _run_loop so tests and manual scripts can
        trigger exactly one pass without spinning up the background task."""
        async with session_scope() as db:
            cursor = await db.get(SyncCursor, "default")
            if cursor is None:
                # First run ever: start from the current ledger rather than
                # 0 — Soroban RPC rejects startLedger=0, and RPC providers
                # only retain a limited event history anyway, so there's
                # nothing meaningful to backfill from the beginning of time.
                start_ledger = await self._client.latest_ledger()
                cursor = SyncCursor(id="default", last_ledger=start_ledger)
                db.add(cursor)
                await db.commit()
            start_ledger = cursor.last_ledger

        events = await self._client.fetch_new_events(start_ledger=start_ledger)
        if not events:
            return 0

        async with session_scope() as db:
            for event in events:
                await _ingest_event(db, event)
            cursor = await db.get(SyncCursor, "default")
            cursor.last_ledger = max(e.ledger for e in events) + 1
            await db.commit()

        logger.info("Sync worker ingested %d event(s), cursor now at ledger %d", len(events), cursor.last_ledger)
        return len(events)


async def _ingest_event(db, event: RawContractEvent) -> None:
    db.add(
        ContractEvent(
            ledger=event.ledger,
            event_type=event.event_type,
            topic=event.topic,
            data=json.dumps(event.data),
        )
    )

    if event.event_type in (EVENT_SPEND_APPROVED, EVENT_SPEND_REJECTED):
        await _ingest_spend_event(db, event)
    # Every other confirmed event (daily_cap_updated, per_call_max_updated,
    # kill_switch_toggled, allowlist_entry_added/removed, agent_added/removed,
    # ownership_transfer_proposed/ownership_transferred) is logged to
    # ContractEvent above but doesn't produce a Transaction row — none of
    # them are payment attempts.


async def _ingest_spend_event(db, event: RawContractEvent) -> None:
    """Backfills a Transaction row for a spend that wasn't relayed through
    this backend. Deduped on contract_tx_hash so re-polling the same
    ledger range (or a relay call that also gets picked up here) never
    double-counts a spend.

    Field names here (agent/destination as topics; token, amount plus
    spent_today/remaining_today or reason as data) are confirmed by Bridle
    Contract's README event table; the exact data-payload *wire shape*
    (map vs. positional vec) is not, so `event.data` may need adjusting
    here if it turns out not to decode to a plain dict — see
    soroban_client.py's `_decode_event_value`.
    """
    existing = await db.scalar(select(Transaction).where(Transaction.contract_tx_hash == event.tx_hash))
    if existing is not None:
        return

    topics = event.topic.split(".")  # event_name.agent.destination, per soroban_client._decode_topic
    agent = topics[1] if len(topics) > 1 else None
    destination = topics[2] if len(topics) > 2 else event.data.get("destination", "unknown")

    approved = event.event_type == EVENT_SPEND_APPROVED
    db.add(
        Transaction(
            agent=agent,
            destination=destination,
            token=event.data.get("token", "native"),
            amount=float(event.data.get("amount", 0)),
            status=PaymentStatus.APPROVED if approved else PaymentStatus.REJECTED,
            rejection_reason=None if approved else _map_reason(event.data.get("reason")),
            detail="Backfilled from chain event (not relayed through this backend).",
            contract_tx_hash=event.tx_hash,
            source="sync",
        )
    )


def _map_reason(raw: str | None) -> RejectionReason | None:
    if raw is None:
        return None
    try:
        return RejectionReason(raw)
    except ValueError:
        return RejectionReason.CHAIN_AUTHORIZATION_DENIED
