"""
SQLAlchemy ORM models for Bridle Backend's local log/cache database.

This database is a *read-optimized mirror* of chain activity, not a source
of truth — the Soroban contract remains authoritative for policy and spend
state. Rows here exist so the dashboard has something fast and query-able
to read.
"""
import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, Float, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PaymentStatus(str, enum.Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


class RejectionReason(str, enum.Enum):
    """Structured rejection reasons surfaced to callers and the SDK.

    Adding a new category: append a member here, handle it in
    app/services/policy_service.py, and document it in the README's
    endpoint table. Nothing else needs to change.
    """

    DESTINATION_NOT_ALLOWLISTED = "destination_not_allowlisted"
    PER_CALL_MAX_EXCEEDED = "per_call_max_exceeded"
    DAILY_CAP_EXCEEDED = "daily_cap_exceeded"
    KILL_SWITCH_ACTIVE = "kill_switch_active"
    CHAIN_AUTHORIZATION_DENIED = "chain_authorization_denied"
    UPSTREAM_ERROR = "upstream_error"


class Transaction(Base):
    """One attempted payment, whether it was approved or rejected.

    Rows are written from two places: the /relay endpoint (the normal path)
    and the sync worker (backfilling spends that happened on-chain without
    going through this backend, e.g. manual contract calls during testing).
    `source` distinguishes the two so the log stays honest about provenance.
    """

    __tablename__ = "transactions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)

    # Contract supports multiple registered agents; this is which one
    # requested the payment (its Stellar address). Null for rows backfilled
    # by the sync worker from an event that doesn't carry it cleanly.
    agent: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    destination: Mapped[str] = mapped_column(String(64), index=True)
    token: Mapped[str] = mapped_column(String(64), index=True)
    amount: Mapped[float] = mapped_column(Float)

    status: Mapped[PaymentStatus] = mapped_column(Enum(PaymentStatus), index=True)
    rejection_reason: Mapped[str | None] = mapped_column(Enum(RejectionReason), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Populated once the destination payment itself has been submitted
    # (approved requests only; null for rejections or if submission is
    # still pending/failed after chain authorization succeeded).
    payment_tx_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Hash of the on-chain check_and_record_spend invocation, when one was made.
    contract_tx_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    source: Mapped[str] = mapped_column(String(16), default="relay")  # "relay" | "sync"
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)

    __table_args__ = (
        Index("ix_transactions_status_created_at", "status", "created_at"),
        Index("ix_transactions_destination_created_at", "destination", "created_at"),
    )


class ContractEvent(Base):
    """Raw contract events pulled by the sync worker, kept for audit/debugging.

    Distinct from Transaction: this is the unprocessed event feed (spend
    approved/rejected, policy changed, kill switch toggled); Transaction
    rows are the derived, dashboard-friendly view built from spend events.
    """

    __tablename__ = "contract_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    ledger: Mapped[int] = mapped_column(index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    topic: Mapped[str] = mapped_column(String(256))
    data: Mapped[str] = mapped_column(Text)  # JSON-encoded event payload
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class SyncCursor(Base):
    """Single-row bookmark of the last ledger the sync worker has processed.

    Single-contract, single-cursor by design (see repo's v1 scope note);
    a future multi-contract deployment would key this by contract_id.
    """

    __tablename__ = "sync_cursor"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=lambda: "default")
    last_ledger: Mapped[int] = mapped_column(default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
