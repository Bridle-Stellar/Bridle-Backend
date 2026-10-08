"""
Helpers for the opt-in testnet integration test: fund throwaway accounts
with Friendbot, deploy a fresh Bridle Contract instance from its wasm, and
submit owner-signed transactions. Nothing here is used by the unit tests.
"""
from __future__ import annotations

import hashlib
import socket
import threading
import time
from dataclasses import dataclass

import httpx
from stellar_sdk import Asset, Keypair, SorobanServer, TransactionBuilder, TransactionEnvelope, scval
from stellar_sdk import xdr as stellar_xdr

FRIENDBOT_URL = "https://friendbot.stellar.org"
BASE_FEE = 100


@dataclass(frozen=True)
class Deployment:
    contract_id: str
    owner: Keypair
    agent: Keypair
    merchant: Keypair
    relayer: Keypair
    token: str
    daily_cap: int
    per_call_max: int


def fund(public_key: str) -> None:
    response = httpx.get(FRIENDBOT_URL, params={"addr": public_key}, timeout=60)
    if response.status_code != 200 and "createAccountAlreadyExist" not in response.text:
        raise RuntimeError(f"Friendbot failed for {public_key}: {response.status_code} {response.text[:200]}")


def submit(server: SorobanServer, envelope: TransactionEnvelope, signer: Keypair, *, prepare: bool = True):
    """Prepare (simulate + attach footprint), sign, send, and wait. Returns
    the contract call's return value as a native Python value."""
    if prepare:
        envelope = server.prepare_transaction(envelope)
    envelope.sign(signer)
    sent = server.send_transaction(envelope)
    if sent.status.value == "ERROR":
        raise RuntimeError(f"send_transaction failed: {sent.error_result_xdr}")
    result = server.poll_transaction(sent.hash)
    if result.status.value != "SUCCESS":
        raise RuntimeError(f"Transaction {sent.hash} {result.status.value}")
    meta = stellar_xdr.TransactionMeta.from_xdr(result.result_meta_xdr)
    body = meta.v4 or meta.v3
    return_value = body.soroban_meta.return_value if body and body.soroban_meta else None
    return scval.to_native(return_value) if return_value is not None else None


def deploy(server: SorobanServer, network_passphrase: str, wasm: bytes, *, daily_cap: int, per_call_max: int) -> Deployment:
    owner, agent, merchant, relayer = (Keypair.random() for _ in range(4))
    for kp in (owner, agent, merchant, relayer):
        fund(kp.public_key)

    def tx(source: Keypair) -> TransactionBuilder:
        return TransactionBuilder(server.load_account(source.public_key), network_passphrase, base_fee=BASE_FEE).set_timeout(60)

    submit(server, tx(owner).append_upload_contract_wasm_op(wasm).build(), owner)
    contract_address = submit(
        server, tx(owner).append_create_contract_op(wasm_id=hashlib.sha256(wasm).digest(), address=owner.public_key).build(), owner
    )
    contract_id = contract_address.address

    token = Asset.native().contract_id(network_passphrase)
    submit(
        server,
        tx(owner)
        .append_invoke_contract_function_op(
            contract_id,
            "initialize",
            [
                scval.to_address(owner.public_key),
                scval.to_address(agent.public_key),
                scval.to_address(token),
                scval.to_int128(daily_cap),
                scval.to_int128(per_call_max),
            ],
        )
        .build(),
        owner,
    )
    return Deployment(contract_id, owner, agent, merchant, relayer, token, daily_cap, per_call_max)


def sign_and_submit_xdr(server: SorobanServer, network_passphrase: str, unsigned_xdr: str, signer: Keypair) -> None:
    """What the owner's wallet does with a /policy/* response: sign the
    already-prepared transaction and submit it."""
    envelope = TransactionEnvelope.from_xdr(unsigned_xdr, network_passphrase)
    submit(server, envelope, signer, prepare=False)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_http(url: str, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            httpx.get(url, timeout=1)
            return
        except httpx.TransportError:
            time.sleep(0.1)
    raise RuntimeError(f"{url} did not come up within {timeout}s")


class ServerThread(threading.Thread):
    """Runs an asyncio entrypoint (here: the backend under uvicorn) in its
    own thread and event loop, so the synchronous test and SDK can call it
    over real HTTP."""

    def __init__(self, main):
        super().__init__(daemon=True)
        self._main = main

    def run(self) -> None:
        import asyncio

        asyncio.run(self._main())
