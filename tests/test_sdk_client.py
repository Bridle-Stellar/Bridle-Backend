"""
Unit test for the SDK's local signing step — the one piece of BridleClient
that doesn't go over HTTP and is easy to get wrong (wrong argument order to
authorize_entry, wrong XDR round trip, etc.).
"""
from stellar_sdk import Keypair

from app.services.soroban_client import CONTRACT_FN_CHECK_AND_RECORD_SPEND, TOKEN_FN_TRANSFER, decode_invocation
from bridle_sdk.client import BridleClient
from tests.conftest import TEST_CONTRACT_ID, TEST_NETWORK_PASSPHRASE, TEST_VALID_UNTIL_LEDGER, build_unsigned_entry


def test_sign_produces_valid_entries_for_the_agent():
    agent = Keypair.random()
    destination = Keypair.random().public_key
    token_contract_id = Keypair.random().public_key  # stand-in address, shape only matters here
    client = BridleClient(relay_url="http://example.invalid", agent_secret_key=agent.secret)

    from stellar_sdk import scval

    prepared = {
        "network_passphrase": TEST_NETWORK_PASSPHRASE,
        "spend_valid_until_ledger": TEST_VALID_UNTIL_LEDGER,
        "transfer_valid_until_ledger": TEST_VALID_UNTIL_LEDGER,
        "spend_auth_entry_xdr": build_unsigned_entry(
            TEST_CONTRACT_ID, CONTRACT_FN_CHECK_AND_RECORD_SPEND,
            [scval.to_address(agent.public_key), scval.to_address(destination), scval.to_address(token_contract_id), scval.to_int128(1000)],
            authorizer=agent.public_key,
        ),
        "transfer_auth_entry_xdr": build_unsigned_entry(
            token_contract_id, TOKEN_FN_TRANSFER,
            [scval.to_address(agent.public_key), scval.to_address(destination), scval.to_int128(1000)],
            authorizer=agent.public_key,
        ),
    }

    body = client._sign(prepared, destination, "1000", "native", None)

    spend = decode_invocation(body["spend_auth_entry_xdr"])
    assert spend.contract_id == TEST_CONTRACT_ID
    assert spend.function_name == CONTRACT_FN_CHECK_AND_RECORD_SPEND
    assert spend.authorizer == agent.public_key

    transfer = decode_invocation(body["transfer_auth_entry_xdr"])
    assert transfer.function_name == TOKEN_FN_TRANSFER
    assert transfer.authorizer == agent.public_key
