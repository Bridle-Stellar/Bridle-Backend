"""
Read-only transaction log API for the dashboard (Bridle Frontend).

The frontend never talks to Soroban directly — everything it needs about
spend history and current standing comes from these endpoints. Pagination
is plain limit/offset, which is sufficient for a single-agent, single-owner
v1 deployment's data volumes; if that stops being true, switching to
keyset pagination only touches this file.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_policy_cache
from app.models import PaymentStatus, Transaction
from app.schemas import (
    DestinationSpend,
    SpendSummary,
    StatsBucket,
    StatsResponse,
    TransactionOut,
    TransactionPage,
)
from app.services.policy_cache import PolicyStateCache

router = APIRouter(prefix="/transactions", tags=["transactions"])

MAX_PAGE_SIZE = 200


@router.get(
    "",
    response_model=TransactionPage,
    summary="List transactions",
    description="Paginated transaction log, newest first, with optional filtering by date range, status, and destination.",
)
async def list_transactions(
    db: AsyncSession = Depends(get_db),
    status: PaymentStatus | None = Query(default=None, description="Filter to only approved or only rejected."),
    destination: str | None = Query(default=None, description="Filter to a single destination address."),
    start_date: datetime | None = Query(default=None, description="Inclusive lower bound on created_at (ISO 8601)."),
    end_date: datetime | None = Query(default=None, description="Exclusive upper bound on created_at (ISO 8601)."),
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
) -> TransactionPage:
    filters = _build_filters(status, destination, start_date, end_date)

    count_stmt = select(func.count()).select_from(Transaction).where(*filters)
    total = (await db.execute(count_stmt)).scalar_one()

    stmt = (
        select(Transaction)
        .where(*filters)
        .order_by(Transaction.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await db.execute(stmt)).scalars().all()

    return TransactionPage(
        items=[TransactionOut.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/summary",
    response_model=SpendSummary,
    summary="Current period spend summary",
    description="Total spent, remaining budget, and cap for the current daily period, from live on-chain policy.",
)
async def spend_summary(
    db: AsyncSession = Depends(get_db),
    cache: PolicyStateCache = Depends(get_policy_cache),
) -> SpendSummary:
    policy = await cache.get()
    period_start, period_end = _current_utc_day()

    # spent_today/daily_cap come straight from the contract's
    # get_spend_status()/get_policy() — the chain is authoritative here,
    # same as everywhere else in this backend.
    total_spent = float(policy.spent_today)
    remaining = max(policy.daily_cap - policy.spent_today, 0)

    return SpendSummary(
        period_start=period_start,
        period_end=period_end,
        token=policy.token,
        total_spent=total_spent,
        cap=float(policy.daily_cap),
        remaining=float(remaining),
    )


@router.get(
    "/stats",
    response_model=StatsResponse,
    summary="Aggregate stats",
    description="Spend broken down by destination, plus approval/rejection counts bucketed over time.",
)
async def stats(
    db: AsyncSession = Depends(get_db),
    start_date: datetime | None = Query(default=None),
    end_date: datetime | None = Query(default=None),
    bucket: str = Query(default="day", pattern="^(hour|day)$", description="Time bucket granularity."),
) -> StatsResponse:
    filters = _build_filters(None, None, start_date, end_date)

    by_dest_stmt = (
        select(
            Transaction.destination,
            func.sum(Transaction.amount).label("total"),
            func.count().label("count"),
        )
        .where(*filters, Transaction.status == PaymentStatus.APPROVED)
        .group_by(Transaction.destination)
        .order_by(func.sum(Transaction.amount).desc())
    )
    by_dest_rows = (await db.execute(by_dest_stmt)).all()
    by_destination = [
        DestinationSpend(destination=row.destination, total_spent=float(row.total), payment_count=row.count)
        for row in by_dest_rows
    ]

    rows = (await db.execute(select(Transaction).where(*filters))).scalars().all()
    over_time = _bucket_by_time(rows, bucket)

    return StatsResponse(by_destination=by_destination, over_time=over_time)


@router.get(
    "/{transaction_id}",
    response_model=TransactionOut,
    summary="Get a single transaction",
    responses={404: {"description": "No transaction with that ID."}},
)
async def get_transaction(transaction_id: str, db: AsyncSession = Depends(get_db)) -> TransactionOut:
    tx = await db.get(Transaction, transaction_id)
    if tx is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return TransactionOut.model_validate(tx)


# -- helpers -------------------------------------------------------------------


def _build_filters(status, destination, start_date, end_date) -> list:
    filters = []
    if status is not None:
        filters.append(Transaction.status == status)
    if destination is not None:
        filters.append(Transaction.destination == destination)
    if start_date is not None:
        filters.append(Transaction.created_at >= start_date)
    if end_date is not None:
        filters.append(Transaction.created_at < end_date)
    return filters


def _current_utc_day() -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def _bucket_by_time(rows: list[Transaction], bucket: str) -> list[StatsBucket]:
    buckets: dict[datetime, dict[str, float | int]] = {}
    for tx in rows:
        key = _floor_to_bucket(tx.created_at, bucket)
        entry = buckets.setdefault(key, {"approved_count": 0, "rejected_count": 0, "approved_amount": 0.0})
        if tx.status == PaymentStatus.APPROVED:
            entry["approved_count"] += 1
            entry["approved_amount"] += tx.amount
        else:
            entry["rejected_count"] += 1

    return [
        StatsBucket(
            period_start=key,
            approved_count=entry["approved_count"],
            rejected_count=entry["rejected_count"],
            approved_amount=entry["approved_amount"],
        )
        for key, entry in sorted(buckets.items())
    ]


def _floor_to_bucket(dt: datetime, bucket: str) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if bucket == "hour":
        return dt.replace(minute=0, second=0, microsecond=0)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)
