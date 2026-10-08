"""
End-to-end against real Stellar testnet. Skipped unless you opt in:

    BRIDLE_INTEGRATION=1 \
    BRIDLE_CONTRACT_WASM=../Bridle-Contract/target/wasm32v1-none/release/bridle_contract.wasm \
    pytest tests/integration -v

It deploys a fresh Bridle Contract instance from that wasm with four new
Friendbot-funded throwaway accounts (owner, agent, merchant, relayer), runs
this backend over real HTTP with a real SorobanContractClient, and drives it
with the real SDK. Nothing is faked; no secrets are needed or kept. See
docs/INTEGRATION_TESTING.md.
"""
from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from stellar_sdk import SorobanServer

from app.config import Settings
from app.database import Base, get_db
from app.deps import get_app_settings
from app.main import app
from app.services.policy_cache import PolicyStateCache
from app.services.soroban_client import SorobanContractClient
from bridle_sdk import BridleClient, BridleRejected
from tests.integration import testnet

pytestmark = pytest.mark.skipif(
    os.environ.get("BRIDLE_INTEGRATION") != "1" or not os.environ.get("BRIDLE_CONTRACT_WASM"),
    reason="set BRIDLE_INTEGRATION=1 and BRIDLE_CONTRACT_WASM to run testnet integration tests",
)

RPC_URL = os.environ.get("BRIDLE_INTEGRATION_RPC_URL", "https://soroban-testnet.stellar.org")
NETWORK_PASSPHRASE = os.environ.get("BRIDLE_INTEGRATION_NETWORK_PASSPHRASE", "Test SDF Network ; September 2015")

DAILY_CAP = 1_000_000_000  # 100 XLM
PER_CALL_MAX = 300_000_000  # 30 XLM
PAYMENT = 100_000_000  # 10 XLM


@pytest.fixture(scope="module")
def rpc():
    server = SorobanServer(RPC_URL)
    yield server
    server.close()


@pytest.fixture(scope="module")
def deployment(rpc):
    wasm = Path(os.environ["BRIDLE_CONTRACT_WASM"]).read_bytes()
    d = testnet.deploy(rpc, NETWORK_PASSPHRASE, wasm, daily_cap=DAILY_CAP, per_call_max=PER_CALL_MAX)
    print(f"\nDeployed test contract {d.contract_id} (owner {d.owner.public_key}, agent {d.agent.public_key})")
    return d


@pytest.fixture(scope="module")
def backend(deployment):
    """This backend, configured for the fresh deployment, served by uvicorn
    on a free local port with an in-memory database."""
    import uvicorn

    settings = Settings(
        soroban_rpc_url=RPC_URL,
        network_passphrase=NETWORK_PASSPHRASE,
        bridle_contract_id=deployment.contract_id,
        relayer_secret_key=deployment.relayer.secret,
        sync_worker_enabled=False,
        request_timeout_seconds=30,
    )
    port = testnet.free_port()
    holder = {}

    async def main():
        engine = create_async_engine("sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)

        async def override_get_db():
            async with sessions() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_app_settings] = lambda: settings
        client = SorobanContractClient(settings)
        app.state.soroban_client = client
        app.state.policy_cache = PolicyStateCache(client, settings.policy_cache_ttl_seconds)

        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_level="warning"))
        holder["server"] = server
        await server.serve()
        await client.close()
        await engine.dispose()

    thread = testnet.ServerThread(main)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    testnet.wait_for_http(f"{base_url}/health")
    yield base_url

    holder["server"].should_exit = True
    thread.join(timeout=10)
    app.dependency_overrides.clear()


@pytest.fixture(scope="module")
def http(backend):
    with httpx.Client(base_url=backend, timeout=60) as client:
        yield client


@pytest.fixture(scope="module")
def agent_client(backend, deployment):
    return BridleClient(
        relay_url=backend,
        agent_secret_key=deployment.agent.secret,
        timeout=60,
        contract_id=deployment.contract_id,
        network_passphrase=NETWORK_PASSPHRASE,
    )


def _owner_action(http, rpc, deployment, path: str, body: dict) -> None:
    """Build via the policy write proxy, then sign and submit as the owner's wallet would."""
    response = http.post(path, json={**body, "owner_public_key": deployment.owner.public_key})
    assert response.status_code == 200, response.text
    testnet.sign_and_submit_xdr(rpc, NETWORK_PASSPHRASE, response.json()["xdr"], deployment.owner)


def _policy(http) -> dict:
    response = http.get("/policy", params={"fresh": "true"})
    assert response.status_code == 200, response.text
    return response.json()


# Tests run in file order and build on each other's on-chain state.


def test_01_policy_after_initialize(http, deployment):
    policy = _policy(http)

    assert policy["owner"] == deployment.owner.public_key
    assert policy["agents"] == [deployment.agent.public_key]
    assert policy["token"] == deployment.token
    assert policy["daily_cap"] == DAILY_CAP
    assert policy["per_call_max"] == PER_CALL_MAX
    assert policy["kill_switch_active"] is False
    assert policy["allowlist"] == []
    assert policy["spent_today"] == 0
    assert policy["remaining_today"] == DAILY_CAP


def test_02_allowlist_add_through_write_proxy(http, rpc, deployment):
    _owner_action(http, rpc, deployment, "/policy/allowlist/add", {"destination": deployment.merchant.public_key, "category": "compute"})

    assert _policy(http)["allowlist"] == [{"destination": deployment.merchant.public_key, "category": "compute"}]


def test_03_pay_is_approved_and_recorded(http, agent_client, deployment):
    result = agent_client.pay(destination=deployment.merchant.public_key, amount=str(PAYMENT), token="native")

    assert result.status == "approved"
    assert result.contract_tx_hash and result.payment_tx_hash
    assert result.spent_today == PAYMENT
    assert result.remaining_today == DAILY_CAP - PAYMENT

    policy = _policy(http)
    assert policy["spent_today"] == PAYMENT
    assert policy["remaining_today"] == DAILY_CAP - PAYMENT

    logged = http.get(f"/transactions/{result.transaction_id}").json()
    assert logged["status"] == "approved"
    assert logged["payment_tx_hash"] == result.payment_tx_hash


def test_04_spend_event_decodes(rpc, deployment, backend):
    import asyncio

    from app.services.soroban_client import EVENT_SPEND_APPROVED

    async def fetch():
        client = SorobanContractClient(Settings(soroban_rpc_url=RPC_URL, bridle_contract_id=deployment.contract_id))
        try:
            latest = await client.latest_ledger()
            return await client.fetch_new_events(start_ledger=max(latest - 200, 1))
        finally:
            await client.close()

    events = asyncio.run(fetch())
    [spend] = [e for e in events if e.event_type == EVENT_SPEND_APPROVED]
    assert spend.topic == f"{EVENT_SPEND_APPROVED}.{deployment.agent.public_key}.{deployment.merchant.public_key}"
    assert spend.data["amount"] == PAYMENT
    assert spend.data["token"] == deployment.token


def test_05_over_per_call_max_is_rejected(agent_client, deployment):
    with pytest.raises(BridleRejected) as rejected:
        agent_client.pay(destination=deployment.merchant.public_key, amount=str(PER_CALL_MAX + 1), token="native")
    assert rejected.value.reason == "per_call_max_exceeded"


def test_06_kill_switch_halts_spending(http, rpc, agent_client, deployment):
    _owner_action(http, rpc, deployment, "/policy/kill-switch", {"active": True})
    assert _policy(http)["kill_switch_active"] is True

    with pytest.raises(BridleRejected) as rejected:
        agent_client.pay(destination=deployment.merchant.public_key, amount=str(PAYMENT), token="native")
    assert rejected.value.reason == "kill_switch_active"

    _owner_action(http, rpc, deployment, "/policy/kill-switch", {"active": False})
    assert _policy(http)["kill_switch_active"] is False
