"""
The payment interception endpoint — Bridle's core function.

Split into two calls because of Bridle Contract's confirmed auth model
(see app/schemas.py's module docstring and this repo's README, "Auth
model"): the agent must sign its own authorization for both
check_and_record_spend and the SEP-41 transfer, so there's an unavoidable
round trip to the agent's key holder between "here's what needs signing"
and "here's what I signed." The client SDK hides this behind one
`client.pay(...)` call.

  POST /relay/prepare  — declare a payment, get back unsigned entries to sign.
  POST /relay/submit   — submit the signed entries; this is where policy is
                         actually enforced and the transaction outcome is
                         decided and logged.

NOTE ON STATUS CODES: a policy violation is a client error (the request as
given cannot be authorized under current policy), so /relay/submit returns
403 Forbidden with a structured body — not a generic 500, and not 402
Payment Required (which would mean "no payment was attempted yet").
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database import get_db
from app.deps import get_app_settings, get_policy_cache, get_soroban_client
from app.models import PaymentStatus, RejectionReason, Transaction
from app.schemas import (
    PreparedPayment,
    RejectionDetail,
    RelayPaymentResponse,
    RelayPrepareRequest,
    RelaySubmitRequest,
)
from app.services.policy_cache import PolicyStateCache
from app.services.policy_service import precheck_payment
from app.services.soroban_client import (
    CONTRACT_FN_CHECK_AND_RECORD_SPEND,
    TOKEN_FN_TRANSFER,
    DecodedInvocation,
    SorobanCallError,
    SorobanContractClient,
    decode_invocation,
    resolve_token_contract_id,
)

logger = logging.getLogger("bridle.relay")

router = APIRouter(prefix="/relay", tags=["relay"])


@router.post(
    "/prepare",
    response_model=PreparedPayment,
    responses={403: {"description": "Payment rejected by policy.", "model": RelayPaymentResponse}},
    summary="Prepare a guarded payment for signing",
    description=(
        "Runs the fast local policy pre-check and, if it passes, returns two unsigned Soroban "
        "authorization entries — one for check_and_record_spend, one for the SEP-41 transfer — "
        "for the agent to sign with its own key before calling /relay/submit. If the pre-check "
        "already fails, the attempt is logged and rejected here without touching the chain."
    ),
)
async def prepare_payment(
    payload: RelayPrepareRequest,
    db: AsyncSession = Depends(get_db),
    client: SorobanContractClient = Depends(get_soroban_client),
    cache: PolicyStateCache = Depends(get_policy_cache),
    settings: Settings = Depends(get_app_settings),
) -> PreparedPayment:
    amount = _parse_amount(payload.amount)
    token_contract_id = resolve_token_contract_id(payload.token, settings.network_passphrase)

    policy = await cache.get()
    precheck = precheck_payment(policy, payload.destination, amount)
    if not precheck.approved:
        tx = await _log(db, payload.agent_public_key, payload.destination, payload.token, amount, PaymentStatus.REJECTED, precheck.reason, precheck.message)
        raise _rejection(tx.id, precheck.reason, precheck.message, payload.destination, payload.amount, payload.token)

    try:
        spend_auth = await client.prepare_check_and_record_spend(payload.agent_public_key, payload.destination, token_contract_id, amount)
        transfer_auth = await client.prepare_transfer(payload.agent_public_key, payload.destination, token_contract_id, amount)
    except SorobanCallError as exc:
        logger.exception("Failed to prepare authorization entries")
        raise HTTPException(status_code=502, detail=f"Could not prepare payment: {exc}") from exc

    return PreparedPayment(
        destination=payload.destination,
        amount=payload.amount,
        token=token_contract_id,
        network_passphrase=settings.network_passphrase,
        spend_auth_entry_xdr=spend_auth.entry_xdr,
        spend_valid_until_ledger=spend_auth.valid_until_ledger,
        transfer_auth_entry_xdr=transfer_auth.entry_xdr,
        transfer_valid_until_ledger=transfer_auth.valid_until_ledger,
    )


@router.post(
    "/submit",
    response_model=RelayPaymentResponse,
    responses={403: {"description": "Payment rejected by policy.", "model": RelayPaymentResponse}},
    summary="Submit a signed payment",
    description=(
        "Submits the agent-signed authorization entries from /relay/prepare. Checks them against "
        "on-chain policy via check_and_record_spend and, only if approved, submits the SEP-41 "
        "transfer. Every attempt is logged regardless of outcome."
    ),
)
async def submit_payment(
    payload: RelaySubmitRequest,
    db: AsyncSession = Depends(get_db),
    client: SorobanContractClient = Depends(get_soroban_client),
    cache: PolicyStateCache = Depends(get_policy_cache),
    settings: Settings = Depends(get_app_settings),
) -> RelayPaymentResponse:
    amount = _parse_amount(payload.amount)
    token_contract_id = resolve_token_contract_id(payload.token, settings.network_passphrase)

    spend_invocation = _decode_or_400(payload.spend_auth_entry_xdr)
    transfer_invocation = _decode_or_400(payload.transfer_auth_entry_xdr)
    _validate_spend_invocation(spend_invocation, payload, settings, token_contract_id, amount)
    _validate_transfer_invocation(transfer_invocation, payload, token_contract_id, amount)

    policy = await cache.get()
    precheck = precheck_payment(policy, payload.destination, amount)
    if not precheck.approved:
        tx = await _log(db, payload.agent_public_key, payload.destination, payload.token, amount, PaymentStatus.REJECTED, precheck.reason, precheck.message)
        raise _rejection(tx.id, precheck.reason, precheck.message, payload.destination, payload.amount, payload.token)

    try:
        authorization = await client.submit_check_and_record_spend(payload.spend_auth_entry_xdr)
    except SorobanCallError as exc:
        logger.exception("check_and_record_spend submission failed")
        tx = await _log(
            db, payload.agent_public_key, payload.destination, payload.token, amount, PaymentStatus.REJECTED,
            RejectionReason.UPSTREAM_ERROR, "Could not reach the policy contract.",
        )
        raise _rejection(tx.id, RejectionReason.UPSTREAM_ERROR, "Could not reach the policy contract.", payload.destination, payload.amount, payload.token) from exc

    cache.invalidate()

    if not authorization.authorized:
        tx = await _log(
            db, payload.agent_public_key, payload.destination, payload.token, amount, PaymentStatus.REJECTED,
            RejectionReason.CHAIN_AUTHORIZATION_DENIED, authorization.reason or "Denied by on-chain policy check.",
            contract_tx_hash=authorization.tx_hash or None,
        )
        raise _rejection(tx.id, RejectionReason.CHAIN_AUTHORIZATION_DENIED, authorization.reason, payload.destination, payload.amount, payload.token)

    try:
        payment_hash = await client.submit_transfer(payload.transfer_auth_entry_xdr)
    except SorobanCallError as exc:
        # The spend was already authorized and recorded on-chain; delivery
        # failed afterward. This is not a policy rejection — log it as
        # approved-but-undelivered so it's visible for manual follow-up.
        logger.error("Payment authorized on-chain but delivery failed: %s", exc)
        tx = await _log(
            db, payload.agent_public_key, payload.destination, payload.token, amount, PaymentStatus.APPROVED, None,
            f"Authorized on-chain (tx {authorization.tx_hash}) but delivery failed: {exc}",
            contract_tx_hash=authorization.tx_hash,
        )
        raise HTTPException(
            status_code=502,
            detail={
                "transaction_id": tx.id,
                "message": "Payment was authorized on-chain but could not be delivered. See transaction log for details.",
            },
        ) from exc

    tx = await _log(
        db, payload.agent_public_key, payload.destination, payload.token, amount, PaymentStatus.APPROVED, None, None,
        contract_tx_hash=authorization.tx_hash, payment_tx_hash=payment_hash,
    )
    return RelayPaymentResponse(
        status=PaymentStatus.APPROVED,
        transaction_id=tx.id,
        destination=payload.destination,
        amount=payload.amount,
        token=payload.token,
        payment_tx_hash=payment_hash,
        contract_tx_hash=authorization.tx_hash,
        spent_today=authorization.spent_today,
        remaining_today=authorization.remaining_today,
    )


# -- validation -----------------------------------------------------------------


def _parse_amount(amount: str) -> int:
    try:
        value = int(amount)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"amount must be an integer string in the token's smallest unit, got {amount!r}") from exc
    if value <= 0:
        raise HTTPException(status_code=400, detail="amount must be positive")
    return value


def _decode_or_400(entry_xdr: str) -> DecodedInvocation:
    try:
        return decode_invocation(entry_xdr)
    except Exception as exc:  # noqa: BLE001 - any malformed XDR is a client error here
        raise HTTPException(status_code=400, detail=f"Could not decode authorization entry: {exc}") from exc


def _validate_spend_invocation(decoded: DecodedInvocation, payload: RelaySubmitRequest, settings: Settings, token_contract_id: str, amount: int) -> None:
    expected = (settings.bridle_contract_id, CONTRACT_FN_CHECK_AND_RECORD_SPEND, [payload.agent_public_key, payload.destination, token_contract_id, amount], payload.agent_public_key)
    actual = (decoded.contract_id, decoded.function_name, decoded.args, decoded.authorizer)
    if actual != expected:
        raise HTTPException(status_code=400, detail="Signed spend authorization does not match the declared payment.")


def _validate_transfer_invocation(decoded: DecodedInvocation, payload: RelaySubmitRequest, token_contract_id: str, amount: int) -> None:
    expected = (token_contract_id, TOKEN_FN_TRANSFER, [payload.agent_public_key, payload.destination, amount], payload.agent_public_key)
    actual = (decoded.contract_id, decoded.function_name, decoded.args, decoded.authorizer)
    if actual != expected:
        raise HTTPException(status_code=400, detail="Signed transfer authorization does not match the declared payment.")


# -- logging ----------------------------------------------------------------------


async def _log(
    db: AsyncSession,
    agent: str | None,
    destination: str,
    token: str,
    amount: int,
    status: PaymentStatus,
    reason: RejectionReason | None,
    detail: str | None,
    *,
    contract_tx_hash: str | None = None,
    payment_tx_hash: str | None = None,
) -> Transaction:
    tx = Transaction(
        agent=agent,
        destination=destination,
        token=token,
        amount=float(amount),
        status=status,
        rejection_reason=reason,
        detail=detail,
        contract_tx_hash=contract_tx_hash,
        payment_tx_hash=payment_tx_hash,
        source="relay",
    )
    db.add(tx)
    await db.commit()
    await db.refresh(tx)
    return tx


def _rejection(transaction_id: str, reason: RejectionReason | None, message: str | None, destination: str, amount: str, token: str) -> HTTPException:
    return HTTPException(
        status_code=403,
        detail=RelayPaymentResponse(
            status=PaymentStatus.REJECTED,
            transaction_id=transaction_id,
            destination=destination,
            amount=amount,
            token=token,
            rejection=RejectionDetail(reason=reason, message=message or "Payment rejected."),
        ).model_dump(mode="json"),
    )
