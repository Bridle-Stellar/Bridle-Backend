"""
GET /policy: serialization, cache vs ?fresh=true, and failure handling.

The main path feeds real get_policy()/get_spend_status() XDR from
Bridle-Contract's testnet deployment through the real decoders, so this
covers chain bytes -> JSON end to end with only the RPC call faked.
"""
import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timezone

import pytest
from stellar_sdk import xdr as stellar_xdr

from app.config import Settings
from app.deps import get_app_settings
from app.main import app
from app.services.policy_cache import PolicyStateCache
from app.services.soroban_client import (
    AllowlistEntry,
    PolicyView,
    SorobanCallError,
    _parse_policy_snapshot,
    _parse_spend_status,
)
from tests.fixtures import testnet_interface as real

TESTNET_VIEW = PolicyView(
    snapshot=_parse_policy_snapshot(stellar_xdr.SCVal.from_xdr(real.GET_POLICY_RESULT)),
    status=_parse_spend_status(stellar_xdr.SCVal.from_xdr(real.GET_SPEND_STATUS_RESULT)),
    fetched_at=datetime(2026, 10, 8, 15, 30, tzinfo=timezone.utc),
)


@dataclass
class ViewClient:
    """Serves whatever `view` currently is, counting chain reads."""

    view: PolicyView
    reads: int = 0
    error: Exception | None = None
    delay: float = 0.0

    async def get_policy_view(self) -> PolicyView:
        self.reads += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.view


@pytest.fixture
def chain(client):
    """Swaps the app's policy cache for one with a real TTL over a ViewClient.
    Depends on `client` so it runs after (and overrides) its app.state setup."""
    fake = ViewClient(view=TESTNET_VIEW)
    app.state.policy_cache = PolicyStateCache(fake, ttl_seconds=60)
    return fake


def _with_kill_switch(view: PolicyView, active: bool) -> PolicyView:
    return replace(view, snapshot=replace(view.snapshot, kill_switch_active=active))


async def test_returns_full_policy_from_testnet_values(client, chain):
    response = await client.get("/policy")

    assert response.status_code == 200
    assert response.json() == {
        "owner": real.OWNER,
        "agents": [real.AGENT],
        "token": real.TOKEN,
        "daily_cap": 10_000_000_000,
        "per_call_max": 3_000_000_000,
        "kill_switch_active": False,
        "allowlist": [{"destination": real.MERCHANT, "category": "compute"}],
        "spent_today": 500_000_000,
        "remaining_today": 9_500_000_000,
        "period_start": "2026-10-08T00:00:00Z",
        "fetched_at": "2026-10-08T15:30:00Z",
    }


async def test_amounts_are_json_integers(client, chain):
    body = (await client.get("/policy")).json()
    for key in ("daily_cap", "per_call_max", "spent_today", "remaining_today"):
        assert type(body[key]) is int, key


async def test_kill_switch_on_is_reflected(client, chain):
    chain.view = _with_kill_switch(TESTNET_VIEW, True)

    body = (await client.get("/policy")).json()

    assert body["kill_switch_active"] is True


async def test_allowlist_serializes_every_entry_in_order(client, chain):
    entries = [AllowlistEntry(destination=real.MERCHANT, category="compute"), AllowlistEntry(destination=real.OWNER, category="data")]
    chain.view = replace(TESTNET_VIEW, snapshot=replace(TESTNET_VIEW.snapshot, allowlist=entries))

    body = (await client.get("/policy")).json()

    assert body["allowlist"] == [
        {"destination": real.MERCHANT, "category": "compute"},
        {"destination": real.OWNER, "category": "data"},
    ]


async def test_empty_allowlist_and_negative_remaining(client, chain):
    chain.view = replace(
        TESTNET_VIEW,
        snapshot=replace(TESTNET_VIEW.snapshot, allowlist=[]),
        status=replace(TESTNET_VIEW.status, remaining_today=-25),
    )

    body = (await client.get("/policy")).json()

    assert body["allowlist"] == []
    assert body["remaining_today"] == -25


async def test_second_read_is_served_from_cache(client, chain):
    await client.get("/policy")
    await client.get("/policy")

    assert chain.reads == 1


async def test_fresh_bypasses_cache_and_sees_a_new_kill_switch(client, chain):
    assert (await client.get("/policy")).json()["kill_switch_active"] is False

    chain.view = _with_kill_switch(TESTNET_VIEW, True)  # owner flips it on-chain

    cached = (await client.get("/policy")).json()
    fresh = (await client.get("/policy", params={"fresh": "true"})).json()

    assert cached["kill_switch_active"] is False  # stale within the TTL, by design
    assert fresh["kill_switch_active"] is True
    assert chain.reads == 2


async def test_fresh_read_refreshes_the_cache_for_later_reads(client, chain):
    await client.get("/policy")
    chain.view = _with_kill_switch(TESTNET_VIEW, True)
    await client.get("/policy", params={"fresh": "true"})

    later = (await client.get("/policy")).json()

    assert later["kill_switch_active"] is True
    assert chain.reads == 2


@pytest.mark.parametrize(
    "error",
    [SorobanCallError("get_policy simulation failed: HostError"), ConnectionError("rpc unreachable")],
)
async def test_chain_failure_returns_502_not_a_default_policy(client, chain, error):
    chain.error = error

    response = await client.get("/policy")

    assert response.status_code == 502
    body = response.json()
    assert body["detail"]["error"] == "policy_unavailable"
    assert body["detail"]["message"]
    assert "kill_switch_active" not in body


async def test_fresh_failure_does_not_fall_back_to_cached_value(client, chain):
    assert (await client.get("/policy")).status_code == 200

    chain.error = SorobanCallError("rpc down")
    response = await client.get("/policy", params={"fresh": "true"})

    assert response.status_code == 502


async def test_decoder_rejecting_chain_data_returns_502(client, chain):
    # A shape the strict decoder refuses (e.g. the contract renamed a field)
    # surfaces as SorobanCallError from the read and must not become a 200.
    chain.error = SorobanCallError("PolicySnapshot is missing field 'kill_switch'")

    response = await client.get("/policy")

    assert response.status_code == 502
    assert "kill_switch" in response.json()["detail"]["message"]


async def test_slow_chain_read_times_out_with_502(client, chain):
    chain.delay = 0.5
    app.dependency_overrides[get_app_settings] = lambda: Settings(request_timeout_seconds=0.05)

    response = await client.get("/policy")

    assert response.status_code == 502
    assert "Timed out" in response.json()["detail"]["message"]
