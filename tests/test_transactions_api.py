"""
Tests for the transaction log API: filtering, pagination, and the
aggregate summary/stats calculations.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import insert

from app.models import PaymentStatus, RejectionReason, Transaction
from tests.conftest import default_policy

pytestmark = pytest.mark.asyncio

DEST_A = "GAAAA0000000000000000000000000000000000000000000000000"
DEST_B = "GBBBB0000000000000000000000000000000000000000000000000"


async def _seed(session_factory, rows: list[dict]) -> None:
    async with session_factory() as session:
        for row in rows:
            await session.execute(insert(Transaction).values(**row))
        await session.commit()


def _row(destination, amount, status, created_at, reason=None) -> dict:
    return dict(
        id=f"{destination}-{created_at.isoformat()}-{amount}",
        destination=destination,
        token="native",
        amount=amount,
        status=status,
        rejection_reason=reason,
        detail=None,
        payment_tx_hash=None,
        contract_tx_hash=None,
        source="relay",
        created_at=created_at,
    )


async def test_list_transactions_pagination(client, session_factory):
    now = datetime.now(timezone.utc)
    rows = [_row(DEST_A, 1.0, PaymentStatus.APPROVED, now - timedelta(minutes=i)) for i in range(5)]
    await _seed(session_factory, rows)

    page1 = (await client.get("/transactions", params={"limit": 2, "offset": 0})).json()
    assert page1["total"] == 5
    assert len(page1["items"]) == 2

    page2 = (await client.get("/transactions", params={"limit": 2, "offset": 2})).json()
    assert len(page2["items"]) == 2
    assert page1["items"][0]["id"] != page2["items"][0]["id"]


async def test_list_transactions_filters_by_status_and_destination(client, session_factory):
    now = datetime.now(timezone.utc)
    await _seed(
        session_factory,
        [
            _row(DEST_A, 1.0, PaymentStatus.APPROVED, now),
            _row(DEST_A, 2.0, PaymentStatus.REJECTED, now, RejectionReason.PER_CALL_MAX_EXCEEDED),
            _row(DEST_B, 3.0, PaymentStatus.APPROVED, now),
        ],
    )

    approved_only = (await client.get("/transactions", params={"status": "approved"})).json()
    assert approved_only["total"] == 2

    dest_a_only = (await client.get("/transactions", params={"destination": DEST_A})).json()
    assert dest_a_only["total"] == 2

    both = (await client.get("/transactions", params={"status": "rejected", "destination": DEST_A})).json()
    assert both["total"] == 1
    assert both["items"][0]["rejection_reason"] == "per_call_max_exceeded"


async def test_list_transactions_filters_by_date_range(client, session_factory):
    old = datetime.now(timezone.utc) - timedelta(days=2)
    recent = datetime.now(timezone.utc)
    await _seed(session_factory, [_row(DEST_A, 1.0, PaymentStatus.APPROVED, old), _row(DEST_A, 1.0, PaymentStatus.APPROVED, recent)])

    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    result = (await client.get("/transactions", params={"start_date": cutoff})).json()
    assert result["total"] == 1


async def test_stats_by_destination_sums_only_approved(client, session_factory):
    now = datetime.now(timezone.utc)
    await _seed(
        session_factory,
        [
            _row(DEST_A, 10.0, PaymentStatus.APPROVED, now),
            _row(DEST_A, 5.0, PaymentStatus.APPROVED, now),
            _row(DEST_A, 999.0, PaymentStatus.REJECTED, now, RejectionReason.DAILY_CAP_EXCEEDED),
            _row(DEST_B, 1.0, PaymentStatus.APPROVED, now),
        ],
    )

    result = (await client.get("/transactions/stats")).json()
    by_dest = {row["destination"]: row for row in result["by_destination"]}
    assert by_dest[DEST_A]["total_spent"] == 15.0
    assert by_dest[DEST_A]["payment_count"] == 2
    assert by_dest[DEST_B]["total_spent"] == 1.0


async def test_stats_over_time_buckets_by_day(client, session_factory):
    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(days=1)
    await _seed(
        session_factory,
        [
            _row(DEST_A, 10.0, PaymentStatus.APPROVED, now),
            _row(DEST_A, 5.0, PaymentStatus.REJECTED, now, RejectionReason.DAILY_CAP_EXCEEDED),
            _row(DEST_A, 1.0, PaymentStatus.APPROVED, yesterday),
        ],
    )

    result = (await client.get("/transactions/stats")).json()
    assert len(result["over_time"]) == 2
    today_bucket = max(result["over_time"], key=lambda b: b["period_start"])
    assert today_bucket["approved_count"] == 1
    assert today_bucket["rejected_count"] == 1
    assert today_bucket["approved_amount"] == 10.0


async def test_summary_reflects_live_policy_state(client, fake_client):
    fake_client.policy = default_policy(spent_today=3_000_000, daily_cap=10_000_000)

    result = (await client.get("/transactions/summary")).json()
    assert result["total_spent"] == 3_000_000
    assert result["cap"] == 10_000_000
    assert result["remaining"] == 7_000_000


async def test_get_single_transaction_not_found(client):
    response = await client.get("/transactions/does-not-exist")
    assert response.status_code == 404
