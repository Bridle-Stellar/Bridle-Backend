"""
Integration tests for the two-step relay flow (/relay/prepare then
/relay/submit) against a faked Soroban client. Signing uses real
stellar_sdk crypto (tests/conftest.py:sign_entry) against real,
correctly-shaped unsigned entries, so the actual decode/validate code in
app/routers/relay.py is exercised end to end — only the network
submission itself is faked.
"""
import pytest
from stellar_sdk import Asset, Keypair

from tests.conftest import ALLOWLISTED_DESTINATION, OTHER_DESTINATION, TEST_NETWORK_PASSPHRASE, default_policy, sign_entry

DEST = ALLOWLISTED_DESTINATION
OTHER = OTHER_DESTINATION
AGENT = Keypair.random()
NATIVE_CONTRACT_ID = Asset.native().contract_id(TEST_NETWORK_PASSPHRASE)

pytestmark = pytest.mark.asyncio


async def _prepare(client, destination=DEST, amount="1000000", token="native"):
    return await client.post(
        "/relay/prepare",
        json={"agent_public_key": AGENT.public_key, "destination": destination, "amount": amount, "token": token},
    )


async def _submit(client, prepared: dict, agent_public_key=None, destination=DEST, amount="1000000", token="native"):
    return await client.post(
        "/relay/submit",
        json={
            "agent_public_key": agent_public_key or AGENT.public_key,
            "destination": destination,
            "amount": amount,
            "token": token,
            "spend_auth_entry_xdr": sign_entry(prepared["spend_auth_entry_xdr"], AGENT),
            "transfer_auth_entry_xdr": sign_entry(prepared["transfer_auth_entry_xdr"], AGENT),
        },
    )


async def test_approved_payment_end_to_end(client, fake_client):
    prepared = (await _prepare(client)).json()
    assert prepared["token"] == NATIVE_CONTRACT_ID  # "native" resolved to its Stellar Asset Contract

    response = await _submit(client, prepared)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "approved"
    assert body["payment_tx_hash"]
    assert body["contract_tx_hash"]
    assert body["spent_today"] == 1_000_000
    assert len(fake_client.spend_calls) == 1
    assert len(fake_client.transfer_calls) == 1


async def test_prepare_rejects_by_local_precheck_before_touching_chain(client, fake_client):
    response = await _prepare(client, destination=OTHER)

    assert response.status_code == 403
    detail = response.json()["detail"]
    assert detail["rejection"]["reason"] == "destination_not_allowlisted"


async def test_prepare_rejects_over_daily_cap(client, fake_client):
    fake_client.policy = default_policy(spent_today=9_500_000, daily_cap=10_000_000)

    response = await _prepare(client)

    assert response.status_code == 403
    assert response.json()["detail"]["rejection"]["reason"] == "daily_cap_exceeded"


async def test_prepare_rejects_when_kill_switch_active(client, fake_client):
    fake_client.policy = default_policy(kill_switch_active=True)

    response = await _prepare(client)

    assert response.status_code == 403
    assert response.json()["detail"]["rejection"]["reason"] == "kill_switch_active"


async def test_submit_rejects_tampered_amount(client, fake_client):
    """The signed entries commit to amount=1_000_000; claiming a different
    amount in the plaintext body must be caught, not silently accepted."""
    prepared = (await _prepare(client)).json()

    response = await _submit(client, prepared, amount="9999999")

    assert response.status_code == 400


async def test_submit_rejects_when_chain_denies_authorization(client, fake_client):
    fake_client.authorize = False
    fake_client.authorization_reason = "contract-level denial"
    prepared = (await _prepare(client)).json()

    response = await _submit(client, prepared)

    assert response.status_code == 403
    assert response.json()["detail"]["rejection"]["reason"] == "chain_authorization_denied"

    log = await client.get("/transactions")
    assert log.json()["items"][0]["rejection_reason"] == "chain_authorization_denied"


async def test_every_attempt_is_logged_regardless_of_outcome(client, fake_client):
    await _prepare(client, destination=OTHER)  # rejected at prepare time, before any chain call

    log = await client.get("/transactions")
    assert log.json()["total"] == 1
    assert log.json()["items"][0]["status"] == "rejected"


async def test_non_integer_amount_is_a_400(client):
    response = await _prepare(client, amount="not-a-number")
    assert response.status_code == 400
