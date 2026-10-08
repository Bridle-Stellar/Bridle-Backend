"""
Shared test fixtures.

Tests never touch a real Soroban RPC endpoint or a real database file:
- `FakeSorobanClient` stands in for SorobanContractClient. Its *prepare*
  methods build real, correctly-shaped unsigned SorobanAuthorizationEntry
  XDRs (the same structure a live simulate_transaction call would return),
  so the actual signing (via stellar_sdk.auth.authorize_entry) and
  decoding (app.services.soroban_client.decode_invocation) code paths in
  the relay router are exercised for real. Only its *submit* methods
  (real network calls) are faked with canned results.
- The database is an in-memory SQLite engine, fresh per test.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from stellar_sdk import Address, Keypair
from stellar_sdk import xdr as stellar_xdr
from stellar_sdk.strkey import StrKey

from app.config import Settings
from app.database import Base, get_db
from app.deps import get_app_settings
from app.main import app
from app.services.policy_cache import PolicyStateCache
from app.services.soroban_client import (
    AllowlistEntry,
    PolicySnapshot,
    PolicyState,
    PolicyView,
    PreparedAuthorization,
    SpendAuthorization,
    SpendStatus,
)

TEST_NETWORK_PASSPHRASE = "Test SDF Network ; September 2015"
TEST_CONTRACT_ID = StrKey.encode_contract(os.urandom(32))
TEST_VALID_UNTIL_LEDGER = 1_000_000
TEST_OWNER = Keypair.random().public_key
TEST_PERIOD_START = 1791417600  # 2026-10-08T00:00:00Z

# Real, StrKey-valid addresses — needed anywhere a value actually flows
# through Address()/scval encoding (the relay flow), unlike the
# database-only tests where any distinct string works fine.
ALLOWLISTED_DESTINATION = Keypair.random().public_key
OTHER_DESTINATION = Keypair.random().public_key


def build_unsigned_entry(contract_id: str, function_name: str, args: list, authorizer: str) -> str:
    """Builds an unsigned SorobanAuthorizationEntry XDR — the same shape a
    real simulate_transaction call returns in `results[0].auth` — without
    needing a network round trip. Used to fake SorobanContractClient's
    prepare_* methods."""
    invoked = stellar_xdr.InvokeContractArgs(
        contract_address=Address(contract_id).to_xdr_sc_address(),
        function_name=stellar_xdr.SCSymbol(function_name.encode()),
        args=args,
    )
    function = stellar_xdr.SorobanAuthorizedFunction(
        type=stellar_xdr.SorobanAuthorizedFunctionType.SOROBAN_AUTHORIZED_FUNCTION_TYPE_CONTRACT_FN,
        contract_fn=invoked,
    )
    invocation = stellar_xdr.SorobanAuthorizedInvocation(function=function, sub_invocations=[])
    address_credentials = stellar_xdr.SorobanAddressCredentials(
        address=Address(authorizer).to_xdr_sc_address(),
        nonce=stellar_xdr.Int64(42),
        signature_expiration_ledger=stellar_xdr.Uint32(0),
        signature=stellar_xdr.SCVal(type=stellar_xdr.SCValType.SCV_VOID),
    )
    credentials = stellar_xdr.SorobanCredentials(
        type=stellar_xdr.SorobanCredentialsType.SOROBAN_CREDENTIALS_ADDRESS_V2,
        address_v2=address_credentials,
    )
    entry = stellar_xdr.SorobanAuthorizationEntry(root_invocation=invocation, credentials=credentials)
    return entry.to_xdr()


def sign_entry(entry_xdr: str, signer: Keypair, valid_until_ledger: int = TEST_VALID_UNTIL_LEDGER) -> str:
    from stellar_sdk.auth import authorize_entry

    return authorize_entry(entry_xdr, signer, valid_until_ledger, TEST_NETWORK_PASSPHRASE).to_xdr()


@dataclass
class FakeSorobanClient:
    """Drop-in replacement for SorobanContractClient in tests."""

    policy: PolicyState
    authorize: bool = True
    authorization_reason: str | None = None
    spend_calls: list[str] = field(default_factory=list)
    transfer_calls: list[str] = field(default_factory=list)

    policy_reads: int = 0
    read_error: Exception | None = None

    async def get_policy_view(self) -> PolicyView:
        """Expands `policy` into the full get_policy()/get_spend_status() pair."""
        self.policy_reads += 1
        if self.read_error is not None:
            raise self.read_error
        p = self.policy
        return PolicyView(
            snapshot=PolicySnapshot(
                owner=TEST_OWNER,
                agents=[],
                token=p.token,
                daily_cap=p.daily_cap,
                per_call_max=p.per_call_max,
                kill_switch_active=p.kill_switch_active,
                allowlist=[AllowlistEntry(destination=d, category="general") for d in p.allowlist],
            ),
            status=SpendStatus(
                daily_cap=p.daily_cap,
                period_start=TEST_PERIOD_START,
                spent_today=p.spent_today,
                remaining_today=p.daily_cap - p.spent_today,
            ),
            fetched_at=datetime.now(timezone.utc),
        )

    async def get_policy_state(self) -> PolicyState:
        return (await self.get_policy_view()).to_state()

    async def prepare_check_and_record_spend(self, agent, destination, token_contract_id, amount) -> PreparedAuthorization:
        from stellar_sdk import scval

        from app.services.soroban_client import CONTRACT_FN_CHECK_AND_RECORD_SPEND

        entry_xdr = build_unsigned_entry(
            TEST_CONTRACT_ID, CONTRACT_FN_CHECK_AND_RECORD_SPEND,
            [scval.to_address(agent), scval.to_address(destination), scval.to_address(token_contract_id), scval.to_int128(amount)],
            authorizer=agent,
        )
        return PreparedAuthorization(entry_xdr=entry_xdr, valid_until_ledger=TEST_VALID_UNTIL_LEDGER)

    async def prepare_transfer(self, agent, destination, token_contract_id, amount) -> PreparedAuthorization:
        from stellar_sdk import scval

        from app.services.soroban_client import TOKEN_FN_TRANSFER

        entry_xdr = build_unsigned_entry(
            token_contract_id, TOKEN_FN_TRANSFER,
            [scval.to_address(agent), scval.to_address(destination), scval.to_int128(amount)],
            authorizer=agent,
        )
        return PreparedAuthorization(entry_xdr=entry_xdr, valid_until_ledger=TEST_VALID_UNTIL_LEDGER)

    async def submit_check_and_record_spend(self, signed_entry_xdr: str) -> SpendAuthorization:
        self.spend_calls.append(signed_entry_xdr)
        if not self.authorize:
            return SpendAuthorization(authorized=False, tx_hash="", reason=self.authorization_reason or "denied on-chain")

        from stellar_sdk import scval

        entry = stellar_xdr.SorobanAuthorizationEntry.from_xdr(signed_entry_xdr)
        amount = int(scval.to_native(entry.root_invocation.function.contract_fn.args[3]))
        self.policy = PolicyState(
            allowlist=self.policy.allowlist,
            per_call_max=self.policy.per_call_max,
            daily_cap=self.policy.daily_cap,
            spent_today=self.policy.spent_today + amount,
            kill_switch_active=self.policy.kill_switch_active,
            token=self.policy.token,
        )
        return SpendAuthorization(
            authorized=True, tx_hash="spendhash" + str(len(self.spend_calls)),
            spent_today=self.policy.spent_today, remaining_today=self.policy.daily_cap - self.policy.spent_today,
        )

    async def submit_transfer(self, signed_entry_xdr: str) -> str:
        self.transfer_calls.append(signed_entry_xdr)
        return "transferhash" + str(len(self.transfer_calls))

    async def fetch_new_events(self, start_ledger: int, limit: int = 100):
        return []

    async def close(self) -> None:
        pass


def default_policy(**overrides) -> PolicyState:
    base = dict(
        allowlist=[ALLOWLISTED_DESTINATION],
        per_call_max=5_000_000,
        daily_cap=10_000_000,
        spent_today=0,
        kill_switch_active=False,
        token="native",
    )
    base.update(overrides)
    return PolicyState(**base)


@pytest_asyncio.fixture
async def db_engine():
    engine = create_async_engine("sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(db_engine):
    return async_sessionmaker(bind=db_engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def fake_client():
    return FakeSorobanClient(policy=default_policy())


@pytest_asyncio.fixture
async def client(session_factory, fake_client):
    """An httpx AsyncClient wired to the FastAPI app with faked chain access
    and an isolated in-memory database, bypassing the app's own lifespan
    (which would try to connect to real infrastructure)."""

    async def override_get_db():
        async with session_factory() as session:
            yield session

    test_settings = Settings(bridle_contract_id=TEST_CONTRACT_ID, network_passphrase=TEST_NETWORK_PASSPHRASE)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_app_settings] = lambda: test_settings
    app.state.soroban_client = fake_client
    app.state.policy_cache = PolicyStateCache(fake_client, ttl_seconds=0)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()
