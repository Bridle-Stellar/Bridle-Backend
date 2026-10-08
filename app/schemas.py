"""
Pydantic request/response models.

These double as the OpenAPI schema (FastAPI generates /docs from them), so
field descriptions here are user-facing documentation, not just comments.
"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models import PaymentStatus, RejectionReason

# ---------------------------------------------------------------------------
# The payment relay: a two-step, x402-*inspired* exchange
# ---------------------------------------------------------------------------
# Bridle Contract's confirmed auth model requires the *agent's own key* to
# authorize both check_and_record_spend and the SEP-41 transfer that follows
# it (see this repo's README, "Auth model"). Neither call can be satisfied
# by a plain JSON payment request the way a classic x402 "pay the amount
# owed" flow works — the agent has to sign a Soroban authorization entry
# for each invocation before this backend can submit anything.
#
# So the exchange is two calls instead of one:
#   1. POST /relay/prepare  — declare the payment; get back two *unsigned*
#      authorization entries (one for check_and_record_spend, one for the
#      transfer) for the agent to sign with its own key.
#   2. POST /relay/submit   — send the *signed* entries back; this backend
#      submits check_and_record_spend first, and only if it's approved,
#      submits the transfer, exactly matching Bridle Contract's documented
#      "the relayer performs the actual transfer once check_and_record_spend
#      returns Approved" sequencing.
# The client SDK (sdk/bridle_sdk) does both calls and the signing in
# between as a single `client.pay(...)`.


class RelayPrepareRequest(BaseModel):
    agent_public_key: str = Field(description="Stellar address (G...) of the registered agent making this payment.")
    destination: str = Field(description="Destination Stellar address (G...) receiving funds.")
    amount: str = Field(description="Amount to pay, as a decimal string in the token's smallest unit.")
    token: str = Field(description="Asset identifier: 'native' for XLM, or a Soroban SEP-41 token contract ID.")
    resource: str | None = Field(default=None, description="Optional identifier of what's being paid for, for logging.")


class RejectionDetail(BaseModel):
    reason: RejectionReason = Field(description="Machine-readable rule that was violated.")
    message: str = Field(description="Human-readable explanation, safe to show to a developer.")


class PreparedPayment(BaseModel):
    """Everything the agent needs to sign before this payment can be submitted."""

    destination: str
    amount: str
    token: str = Field(description="The resolved SEP-41 token contract ID (native XLM's Stellar Asset Contract, if token was 'native').")
    network_passphrase: str
    spend_auth_entry_xdr: str = Field(description="Unsigned authorization entry for check_and_record_spend. Sign with stellar_sdk.auth.authorize_entry().")
    spend_valid_until_ledger: int
    transfer_auth_entry_xdr: str = Field(description="Unsigned authorization entry for the SEP-41 transfer. Sign the same way.")
    transfer_valid_until_ledger: int


class RelaySubmitRequest(BaseModel):
    agent_public_key: str
    destination: str
    amount: str
    token: str = Field(description="Same token identifier passed to /relay/prepare ('native' or a contract ID) — this backend re-resolves it.")
    resource: str | None = None
    spend_auth_entry_xdr: str = Field(description="The signed version of the entry returned by /relay/prepare.")
    transfer_auth_entry_xdr: str = Field(description="The signed version of the entry returned by /relay/prepare.")


class RelayPaymentResponse(BaseModel):
    """Result of a submitted relay attempt. `status` is always present; the rest depends on outcome."""

    status: PaymentStatus
    transaction_id: str = Field(description="ID of the log row for this attempt (see /transactions/{id}).")
    destination: str
    amount: str
    token: str

    rejection: RejectionDetail | None = Field(default=None, description="Present only when status is 'rejected'.")
    payment_tx_hash: str | None = Field(default=None, description="Stellar tx hash of the submitted transfer, once confirmed.")
    contract_tx_hash: str | None = Field(default=None, description="Tx hash of the on-chain check_and_record_spend call.")
    spent_today: float | None = Field(default=None, description="Total spend for the current UTC day, per the contract, after this payment.")
    remaining_today: float | None = Field(default=None, description="Remaining daily budget after this payment.")


# ---------------------------------------------------------------------------
# Transaction log API
# ---------------------------------------------------------------------------


class TransactionOut(BaseModel):
    id: str
    agent: str | None
    destination: str
    token: str
    amount: float
    status: PaymentStatus
    rejection_reason: RejectionReason | None
    detail: str | None
    payment_tx_hash: str | None
    contract_tx_hash: str | None
    source: Literal["relay", "sync"]
    created_at: datetime

    model_config = {"from_attributes": True}


class Page(BaseModel):
    """Generic cursor-free pagination envelope, consistent across list endpoints."""

    items: list
    total: int = Field(description="Total rows matching the filter, independent of page size.")
    limit: int
    offset: int


class TransactionPage(Page):
    items: list[TransactionOut]


class SpendSummary(BaseModel):
    period_start: datetime = Field(description="Start of the current UTC day.")
    period_end: datetime = Field(description="End of the current UTC day.")
    token: str
    total_spent: float = Field(description="spent_today, per the contract's get_spend_status().")
    cap: float = Field(description="daily_cap, per the contract's get_policy().")
    remaining: float = Field(description="remaining_today, per the contract's get_spend_status().")


class DestinationSpend(BaseModel):
    destination: str
    total_spent: float
    payment_count: int


class StatsBucket(BaseModel):
    period_start: datetime
    approved_count: int
    rejected_count: int
    approved_amount: float


class StatsResponse(BaseModel):
    by_destination: list[DestinationSpend]
    over_time: list[StatsBucket]


# ---------------------------------------------------------------------------
# Policy write proxy
# ---------------------------------------------------------------------------
# All owner-only contract calls. Built with the owner as the transaction's
# source account, so the owner's own wallet (e.g. Freighter) signing the
# returned transaction satisfies both the classic envelope signature and
# the contract's owner.require_auth() in one step — see
# app/services/soroban_client.py:build_owner_invocation_xdr.


class UnsignedTransactionEnvelope(BaseModel):
    xdr: str = Field(description="Base64-encoded unsigned TransactionEnvelope XDR.")
    network_passphrase: str
    description: str = Field(description="Human-readable summary of what this transaction does.")


class OwnerActionRequest(BaseModel):
    owner_public_key: str = Field(description="Public key (G...) of the policy owner's wallet that will sign this transaction.")


class SetDailyCapRequest(OwnerActionRequest):
    daily_cap: str = Field(description="New daily spend cap, as a decimal string in the token's smallest unit.")


class SetPerCallMaxRequest(OwnerActionRequest):
    per_call_max: str = Field(description="New per-call maximum, as a decimal string in the token's smallest unit.")


class AddAllowlistEntryRequest(OwnerActionRequest):
    destination: str = Field(description="Stellar address to approve.")
    category: str = Field(
        pattern=r"^[A-Za-z0-9_]{1,32}$",
        description="Category tag, e.g. 'compute', 'data', 'api'. Stored on-chain as a Soroban Symbol: 1-32 chars of a-z, A-Z, 0-9, _.",
    )


class RemoveAllowlistEntryRequest(OwnerActionRequest):
    destination: str = Field(description="Stellar address to revoke.")


class AddAgentRequest(OwnerActionRequest):
    agent: str = Field(description="Stellar address of the agent to register.")


class RemoveAgentRequest(OwnerActionRequest):
    agent: str = Field(description="Stellar address of the agent to deregister. Refused on-chain if it's the last remaining agent.")


class KillSwitchRequest(OwnerActionRequest):
    active: bool = Field(description="True to halt all spending immediately; false to resume normal policy checks.")


class TransferOwnershipRequest(OwnerActionRequest):
    new_owner: str = Field(description="Stellar address of the proposed new owner. Takes effect only once they call accept_ownership.")


class AcceptOwnershipRequest(BaseModel):
    pending_owner_public_key: str = Field(description="Public key of the pending owner accepting the transfer — this wallet signs the returned transaction.")


class ErrorResponse(BaseModel):
    detail: str
