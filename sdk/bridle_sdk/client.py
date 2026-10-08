"""
Minimal client SDK: wraps an agent's payment call and routes it through a
Bridle relay instead of the destination directly.

Adoption is meant to be close to "one import, one line changed":

    from bridle_sdk import BridleClient

    client = BridleClient(relay_url="https://your-bridle-instance", agent_secret_key="S...")
    result = client.pay(destination="G...", amount="1000000", token="native")

Why this needs the agent's own key (not just an HTTP call): Bridle
Contract requires the *agent* to authorize both the policy check
(check_and_record_spend) and the payment transfer itself via Soroban's
native authorization framework — no key besides the agent's own can
satisfy that. This SDK signs those two authorization entries locally with
`agent_secret_key` and never sends that key anywhere; only the resulting
signatures are sent to the relay. See the backend README's "Auth model"
section for the full rationale. This is the one place this SDK can't be
thinner than it is — everything else about talking to Soroban RPC stays
on the backend, per that same design.

The relay is not trusted to choose what gets signed. Before signing,
`_sign` decodes both entries /relay/prepare returned and refuses (raising
BridleVerificationError) unless they authorize exactly the payment this
agent asked for: the SEP-41 `transfer` of `amount` to `destination` on the
requested token, and the matching `check_and_record_spend`, with no nested
authorizations. Pass `contract_id` (and ideally `network_passphrase`) to also
pin which Bridle Contract instance and network the entries are for.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx
from stellar_sdk import Address, Asset, Keypair, scval
from stellar_sdk import xdr as stellar_xdr
from stellar_sdk.auth import authorize_entry

from bridle_sdk.exceptions import BridleRejected, BridleUpstreamError, BridleVerificationError

DEFAULT_TIMEOUT_SECONDS = 10.0


@dataclass
class PaymentResult:
    status: str
    transaction_id: str
    destination: str
    amount: str
    token: str
    payment_tx_hash: str | None = None
    contract_tx_hash: str | None = None
    spent_today: float | None = None
    remaining_today: float | None = None


class BridleClient:
    def __init__(
        self,
        relay_url: str,
        agent_secret_key: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        *,
        contract_id: str | None = None,
        network_passphrase: str | None = None,
    ):
        """
        Args:
            relay_url: Base URL of a running Bridle Backend instance, e.g.
                "https://your-bridle-instance" (no trailing path needed).
            agent_secret_key: Secret seed (S...) for this agent's own
                Stellar keypair — the one registered with Bridle Contract
                via add_agent. Used only to sign locally; never transmitted.
            timeout: Request timeout in seconds per HTTP call. pay() makes
                two (prepare, then submit) — see the backend README's
                latency notes for what's typical per call.
            contract_id: The Bridle Contract instance (C...) this agent is
                registered with. If set, a check_and_record_spend entry for
                any other contract is refused before signing. Recommended:
                without it, a dishonest relay could route the policy check
                to a different contract (the transfer itself is still
                verified either way).
            network_passphrase: If set, a /relay/prepare response for any
                other network is refused before signing.
        """
        self._base_url = relay_url.rstrip("/")
        self._agent = Keypair.from_secret(agent_secret_key)
        self._timeout = timeout
        self._contract_id = contract_id
        self._network_passphrase = network_passphrase

    def pay(self, destination: str, amount: str | int, token: str = "native", resource: str | None = None) -> PaymentResult:
        """Synchronous payment attempt. Raises BridleRejected on policy denial."""
        with httpx.Client(timeout=self._timeout) as http:
            prepared = self._handle_prepare(http.post(f"{self._base_url}/relay/prepare", json=self._prepare_body(destination, amount, token, resource)))
            submit_body = self._sign(prepared, destination, amount, token, resource)
            return self._handle_submit(http.post(f"{self._base_url}/relay/submit", json=submit_body))

    async def pay_async(self, destination: str, amount: str | int, token: str = "native", resource: str | None = None) -> PaymentResult:
        """Async payment attempt, for agent frameworks already running an event loop."""
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            prepared = self._handle_prepare(await http.post(f"{self._base_url}/relay/prepare", json=self._prepare_body(destination, amount, token, resource)))
            submit_body = self._sign(prepared, destination, amount, token, resource)
            return self._handle_submit(await http.post(f"{self._base_url}/relay/submit", json=submit_body))

    # -- internals ------------------------------------------------------------------

    def _prepare_body(self, destination: str, amount: str | int, token: str, resource: str | None) -> dict:
        return {"agent_public_key": self._agent.public_key, "destination": destination, "amount": str(amount), "token": token, "resource": resource}

    def _sign(self, prepared: dict, destination: str, amount: str | int, token: str, resource: str | None) -> dict:
        self._verify_prepared(prepared, destination, amount, token)
        signed_spend = authorize_entry(prepared["spend_auth_entry_xdr"], self._agent, prepared["spend_valid_until_ledger"], prepared["network_passphrase"])
        signed_transfer = authorize_entry(prepared["transfer_auth_entry_xdr"], self._agent, prepared["transfer_valid_until_ledger"], prepared["network_passphrase"])
        return {
            "agent_public_key": self._agent.public_key,
            "destination": destination,
            "amount": str(amount),
            "token": token,
            "resource": resource,
            "spend_auth_entry_xdr": signed_spend.to_xdr(),
            "transfer_auth_entry_xdr": signed_transfer.to_xdr(),
        }

    def _verify_prepared(self, prepared: dict, destination: str, amount: str | int, token: str) -> None:
        """Refuse to sign anything but the payment this agent asked for. See the module docstring."""
        passphrase = prepared["network_passphrase"]
        if self._network_passphrase is not None and passphrase != self._network_passphrase:
            raise BridleVerificationError(f"Relay prepared entries for network {passphrase!r}, expected {self._network_passphrase!r}.")

        agent = self._agent.public_key
        value = int(amount)
        token_contract_id = Asset.native().contract_id(passphrase) if token == "native" else token

        spend = _decode_entry(prepared["spend_auth_entry_xdr"])
        expected_spend = (CHECK_AND_RECORD_SPEND, [agent, destination, token_contract_id, value], agent)
        if (spend.function_name, spend.args, spend.authorizer) != expected_spend:
            raise BridleVerificationError(f"Spend entry does not match the requested payment: {spend}")
        if self._contract_id is not None and spend.contract_id != self._contract_id:
            raise BridleVerificationError(f"Spend entry is for contract {spend.contract_id}, expected {self._contract_id}.")

        transfer = _decode_entry(prepared["transfer_auth_entry_xdr"])
        expected_transfer = (token_contract_id, TRANSFER, [agent, destination, value], agent)
        if (transfer.contract_id, transfer.function_name, transfer.args, transfer.authorizer) != expected_transfer:
            raise BridleVerificationError(f"Transfer entry does not match the requested payment: {transfer}")

    @staticmethod
    def _handle_prepare(response: httpx.Response) -> dict:
        if response.status_code == 403:
            BridleClient._raise_rejection(response)
        if response.status_code >= 400:
            raise BridleUpstreamError(f"Bridle relay returned {response.status_code} from /relay/prepare: {response.text}")
        return response.json()

    @staticmethod
    def _handle_submit(response: httpx.Response) -> PaymentResult:
        if response.status_code == 403:
            BridleClient._raise_rejection(response)
        if response.status_code >= 400:
            raise BridleUpstreamError(f"Bridle relay returned {response.status_code} from /relay/submit: {response.text}")

        data = response.json()
        return PaymentResult(
            status=data["status"],
            transaction_id=data["transaction_id"],
            destination=data["destination"],
            amount=data["amount"],
            token=data["token"],
            payment_tx_hash=data.get("payment_tx_hash"),
            contract_tx_hash=data.get("contract_tx_hash"),
            spent_today=data.get("spent_today"),
            remaining_today=data.get("remaining_today"),
        )

    @staticmethod
    def _raise_rejection(response: httpx.Response) -> None:
        body = response.json().get("detail", {})
        rejection = body.get("rejection") or {}
        raise BridleRejected(
            reason=rejection.get("reason", "unknown"),
            message=rejection.get("message", "Payment rejected."),
            transaction_id=body.get("transaction_id"),
        )

CHECK_AND_RECORD_SPEND = "check_and_record_spend"
TRANSFER = "transfer"


@dataclass(frozen=True)
class _Invocation:
    contract_id: str
    function_name: str
    args: list
    authorizer: str | None


def _decode_entry(entry_xdr: str) -> _Invocation:
    """What an authorization entry commits the signer to. Only a single,
    direct contract call is accepted: an entry with nested authorizations
    (sub-invocations) could authorize more than the call it appears to be."""
    try:
        entry = stellar_xdr.SorobanAuthorizationEntry.from_xdr(entry_xdr)
    except Exception as exc:
        raise BridleVerificationError(f"Relay returned an undecodable authorization entry: {exc}") from exc

    invocation = entry.root_invocation
    if invocation.function.type != stellar_xdr.SorobanAuthorizedFunctionType.SOROBAN_AUTHORIZED_FUNCTION_TYPE_CONTRACT_FN:
        raise BridleVerificationError("Authorization entry is not a contract call.")
    if invocation.sub_invocations:
        raise BridleVerificationError("Authorization entry carries nested authorizations; refusing to sign.")

    call = invocation.function.contract_fn
    credentials = entry.credentials
    if credentials.type == stellar_xdr.SorobanCredentialsType.SOROBAN_CREDENTIALS_ADDRESS:
        authorizer = Address.from_xdr_sc_address(credentials.address.address).address
    elif credentials.type == stellar_xdr.SorobanCredentialsType.SOROBAN_CREDENTIALS_ADDRESS_V2:
        authorizer = Address.from_xdr_sc_address(credentials.address_v2.address).address
    else:
        authorizer = None
    return _Invocation(
        contract_id=Address.from_xdr_sc_address(call.contract_address).address,
        function_name=call.function_name.sc_symbol.decode(),
        args=[_plain(scval.to_native(a)) for a in call.args],
        authorizer=authorizer,
    )


def _plain(value):
    return value.address if isinstance(value, Address) else value
