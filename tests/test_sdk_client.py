"""
The SDK's local signing step: it signs the entries /relay/prepare returns,
but only after checking they authorize exactly the payment the agent asked
for. A relay that asks for anything else must get a BridleVerificationError
and no signature.
"""
import httpx
import pytest
from stellar_sdk import Asset, Keypair, scval
from stellar_sdk import xdr as stellar_xdr

from app.main import app
from app.services.soroban_client import CONTRACT_FN_CHECK_AND_RECORD_SPEND, TOKEN_FN_TRANSFER, PreparedAuthorization, decode_invocation
from bridle_sdk import BridleVerificationError
from bridle_sdk.client import BridleClient
from tests.conftest import (
    ALLOWLISTED_DESTINATION,
    TEST_CONTRACT_ID,
    TEST_NETWORK_PASSPHRASE,
    TEST_VALID_UNTIL_LEDGER,
    build_unsigned_entry,
)

AGENT = Keypair.random()
DEST = Keypair.random().public_key
ATTACKER = Keypair.random().public_key
NATIVE = Asset.native().contract_id(TEST_NETWORK_PASSPHRASE)
AMOUNT = 1000


def _spend_entry(*, contract=TEST_CONTRACT_ID, function=CONTRACT_FN_CHECK_AND_RECORD_SPEND, agent=None, destination=DEST, token=NATIVE, amount=AMOUNT, authorizer=None):
    agent = agent or AGENT.public_key
    return build_unsigned_entry(
        contract, function,
        [scval.to_address(agent), scval.to_address(destination), scval.to_address(token), scval.to_int128(amount)],
        authorizer=authorizer or agent,
    )


def _transfer_entry(*, contract=NATIVE, function=TOKEN_FN_TRANSFER, source=None, destination=DEST, amount=AMOUNT, authorizer=None):
    source = source or AGENT.public_key
    return build_unsigned_entry(
        contract, function,
        [scval.to_address(source), scval.to_address(destination), scval.to_int128(amount)],
        authorizer=authorizer or source,
    )


def _prepared(spend=None, transfer=None, passphrase=TEST_NETWORK_PASSPHRASE) -> dict:
    return {
        "network_passphrase": passphrase,
        "spend_valid_until_ledger": TEST_VALID_UNTIL_LEDGER,
        "transfer_valid_until_ledger": TEST_VALID_UNTIL_LEDGER,
        "spend_auth_entry_xdr": spend or _spend_entry(),
        "transfer_auth_entry_xdr": transfer or _transfer_entry(),
    }


def _client(**kwargs) -> BridleClient:
    return BridleClient(relay_url="http://example.invalid", agent_secret_key=AGENT.secret, **kwargs)


def _with_sub_invocation(entry_xdr: str) -> str:
    entry = stellar_xdr.SorobanAuthorizationEntry.from_xdr(entry_xdr)
    nested = stellar_xdr.SorobanAuthorizationEntry.from_xdr(_transfer_entry(destination=ATTACKER, amount=10**12))
    entry.root_invocation.sub_invocations = [nested.root_invocation]
    return entry.to_xdr()


def test_sign_produces_valid_entries_for_the_agent():
    body = _client(contract_id=TEST_CONTRACT_ID, network_passphrase=TEST_NETWORK_PASSPHRASE)._sign(_prepared(), DEST, str(AMOUNT), "native", None)

    spend = decode_invocation(body["spend_auth_entry_xdr"])
    assert spend.contract_id == TEST_CONTRACT_ID
    assert spend.function_name == CONTRACT_FN_CHECK_AND_RECORD_SPEND
    assert spend.authorizer == AGENT.public_key

    transfer = decode_invocation(body["transfer_auth_entry_xdr"])
    assert transfer.contract_id == NATIVE
    assert transfer.function_name == TOKEN_FN_TRANSFER
    assert transfer.args == [AGENT.public_key, DEST, AMOUNT]
    assert transfer.authorizer == AGENT.public_key


def test_non_native_token_is_matched_by_contract_id():
    token = Keypair.random().public_key  # any address works for shape here
    prepared = _prepared(spend=_spend_entry(token=token), transfer=_transfer_entry(contract=token))
    _client()._sign(prepared, DEST, str(AMOUNT), token, None)


@pytest.mark.parametrize(
    "prepared",
    [
        pytest.param(_prepared(transfer=_transfer_entry(destination=ATTACKER)), id="transfer-to-other-destination"),
        pytest.param(_prepared(transfer=_transfer_entry(amount=AMOUNT * 1000)), id="transfer-larger-amount"),
        pytest.param(_prepared(transfer=_transfer_entry(contract=Keypair.random().public_key)), id="transfer-other-token"),
        pytest.param(_prepared(transfer=_transfer_entry(function="approve")), id="transfer-other-function"),
        pytest.param(_prepared(transfer=_with_sub_invocation(_transfer_entry())), id="transfer-nested-authorization"),
        pytest.param(_prepared(spend=_spend_entry(destination=ATTACKER)), id="spend-other-destination"),
        pytest.param(_prepared(spend=_spend_entry(amount=1)), id="spend-smaller-amount"),
        pytest.param(_prepared(spend=_spend_entry(function="add_agent")), id="spend-other-function"),
        pytest.param(_prepared(spend=_with_sub_invocation(_spend_entry())), id="spend-nested-authorization"),
        pytest.param(_prepared(spend="not-xdr"), id="spend-undecodable"),
    ],
)
def test_tampered_entries_are_refused_before_signing(prepared):
    with pytest.raises(BridleVerificationError):
        _client()._sign(prepared, DEST, str(AMOUNT), "native", None)


def test_entry_for_another_signer_is_refused():
    other = Keypair.random().public_key
    with pytest.raises(BridleVerificationError):
        _client()._sign(_prepared(transfer=_transfer_entry(authorizer=other)), DEST, str(AMOUNT), "native", None)


def test_pinned_contract_id_is_enforced():
    other_contract = Keypair.random().public_key
    prepared = _prepared(spend=_spend_entry(contract=other_contract))

    _client()._sign(prepared, DEST, str(AMOUNT), "native", None)  # unpinned: transfer is still checked, contract isn't
    with pytest.raises(BridleVerificationError, match="contract"):
        _client(contract_id=TEST_CONTRACT_ID)._sign(prepared, DEST, str(AMOUNT), "native", None)


def test_pinned_network_is_enforced():
    with pytest.raises(BridleVerificationError, match="network"):
        _client(network_passphrase="Public Global Stellar Network ; September 2015")._sign(_prepared(), DEST, str(AMOUNT), "native", None)


async def test_tampered_prepare_response_never_reaches_submit(client, fake_client, monkeypatch):
    """End to end through the real app: a relay whose /relay/prepare points the
    transfer at an attacker gets nothing signed and no /relay/submit call."""
    honest_prepare_transfer = fake_client.prepare_transfer

    async def malicious_prepare_transfer(agent, destination, token_contract_id, amount):
        prepared = await honest_prepare_transfer(agent, ATTACKER, token_contract_id, amount * 1000)
        return PreparedAuthorization(entry_xdr=prepared.entry_xdr, valid_until_ledger=prepared.valid_until_ledger)

    fake_client.prepare_transfer = malicious_prepare_transfer
    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "bridle_sdk.client.httpx.AsyncClient",
        lambda **kw: real_async_client(transport=httpx.ASGITransport(app=app), base_url="http://test", **kw),
    )

    sdk = BridleClient(relay_url="http://test", agent_secret_key=AGENT.secret)
    with pytest.raises(BridleVerificationError):
        await sdk.pay_async(destination=ALLOWLISTED_DESTINATION, amount="1000000", token="native")

    assert fake_client.spend_calls == []
    assert fake_client.transfer_calls == []
