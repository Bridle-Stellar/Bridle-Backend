"""
Argument encoding of the policy write proxy, checked against the function
signatures in Bridle-Contract's docs/INTERFACE.md ("Functions").
"""
import pytest
from stellar_sdk import Keypair
from stellar_sdk import xdr as stellar_xdr

OWNER = Keypair.random().public_key
DEST = Keypair.random().public_key


@pytest.fixture
def built(fake_client):
    """Records what the router asks the client to build instead of calling RPC."""
    calls = []

    async def build_owner_invocation_xdr(function_name, parameters, owner_public_key):
        calls.append((function_name, parameters, owner_public_key))
        return "AAAA"

    fake_client.build_owner_invocation_xdr = build_owner_invocation_xdr
    return calls


async def test_allowlist_category_is_encoded_as_symbol(client, built):
    response = await client.post("/policy/allowlist/add", json={"destination": DEST, "category": "compute", "owner_public_key": OWNER})

    assert response.status_code == 200
    [(function_name, (destination, category), owner)] = built
    assert function_name == "add_allowlist_entry"
    assert destination.type == stellar_xdr.SCValType.SCV_ADDRESS
    assert category.type == stellar_xdr.SCValType.SCV_SYMBOL  # contract signature: category: Symbol
    assert category.sym.sc_symbol == b"compute"
    assert owner == OWNER


@pytest.mark.parametrize("category", ["", "has space", "dash-ed", "x" * 33])
async def test_allowlist_category_must_be_a_valid_symbol(client, built, category):
    response = await client.post("/policy/allowlist/add", json={"destination": DEST, "category": category, "owner_public_key": OWNER})

    assert response.status_code == 422
    assert built == []


async def test_caps_are_encoded_as_i128(client, built):
    await client.post("/policy/daily-cap", json={"daily_cap": "10000000000", "owner_public_key": OWNER})
    await client.post("/policy/per-call-max", json={"per_call_max": "3000000000", "owner_public_key": OWNER})

    assert [(name, [p.type for p in params]) for name, params, _ in built] == [
        ("update_daily_cap", [stellar_xdr.SCValType.SCV_I128]),
        ("update_per_call_max", [stellar_xdr.SCValType.SCV_I128]),
    ]
