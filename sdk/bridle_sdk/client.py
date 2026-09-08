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
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx
from stellar_sdk import Keypair
from stellar_sdk.auth import authorize_entry

from bridle_sdk.exceptions import BridleRejected, BridleUpstreamError

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
    def __init__(self, relay_url: str, agent_secret_key: str, timeout: float = DEFAULT_TIMEOUT_SECONDS):
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
        """
        self._base_url = relay_url.rstrip("/")
        self._agent = Keypair.from_secret(agent_secret_key)
        self._timeout = timeout

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
