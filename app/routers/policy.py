"""
Policy read endpoint and write proxy.

GET /policy serializes the contract's own get_policy() + get_spend_status()
for the dashboard. It computes nothing itself, and if the chain read fails
it returns 502 — never a default policy, since a defaulted kill switch
would read as "off".

DESIGN CHOICE (documented per the brief): the frontend does not call the
Soroban contract directly to change policy. Instead it POSTs the intended
change here, this backend builds the corresponding contract-invocation
transaction (simulated/prepared but *unsigned*), and hands the XDR back
for the owner's own wallet (e.g. Freighter) to sign and submit. This keeps
every place that knows the contract's function names and argument
encoding inside this one service instead of duplicated in the frontend.

This backend never holds the *owner's* key and cannot change policy on
its own — only the connected wallet's signature can authorize a policy
change on-chain. The relayer key (see app/services/soroban_client.py)
is unrelated to and cannot substitute for this.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from stellar_sdk import scval

from app.config import Settings
from app.deps import get_app_settings, get_policy_cache, get_soroban_client
from app.schemas import (
    AcceptOwnershipRequest,
    AddAgentRequest,
    AddAllowlistEntryRequest,
    AllowlistEntryOut,
    KillSwitchRequest,
    PolicyResponse,
    PolicyUnavailableResponse,
    RemoveAgentRequest,
    RemoveAllowlistEntryRequest,
    SetDailyCapRequest,
    SetPerCallMaxRequest,
    TransferOwnershipRequest,
    UnsignedTransactionEnvelope,
)
from app.services.policy_cache import PolicyStateCache
from app.services.soroban_client import (
    CONTRACT_FN_ACCEPT_OWNERSHIP,
    CONTRACT_FN_ADD_AGENT,
    CONTRACT_FN_ADD_ALLOWLIST_ENTRY,
    CONTRACT_FN_REMOVE_AGENT,
    CONTRACT_FN_REMOVE_ALLOWLIST_ENTRY,
    CONTRACT_FN_SET_KILL_SWITCH,
    CONTRACT_FN_TRANSFER_OWNERSHIP,
    CONTRACT_FN_UPDATE_DAILY_CAP,
    CONTRACT_FN_UPDATE_PER_CALL_MAX,
    SorobanContractClient,
)

logger = logging.getLogger("bridle.policy")

router = APIRouter(prefix="/policy", tags=["policy"])


@router.get(
    "",
    response_model=PolicyResponse,
    responses={502: {"description": "The policy could not be read from the chain.", "model": PolicyUnavailableResponse}},
    summary="Current on-chain policy and today's spend",
    description=(
        "The contract's get_policy() and get_spend_status(), as-is: owner, agents, token, caps, kill switch, "
        "allowlist, and today's spend. Served from the short-TTL policy cache unless `fresh=true`. "
        "If the chain can't be read, returns 502; it never returns a default or partial policy."
    ),
)
async def get_policy(
    fresh: bool = Query(default=False, description="Bypass the cache and read the chain now. Use before acting on kill_switch_active."),
    cache: PolicyStateCache = Depends(get_policy_cache),
    settings: Settings = Depends(get_app_settings),
) -> PolicyResponse:
    try:
        view = await asyncio.wait_for(cache.get_view(force_refresh=fresh), timeout=settings.request_timeout_seconds)
    except Exception as exc:  # noqa: BLE001 - any failure to read the chain is a 502, never a fallback policy
        logger.exception("Could not read policy from the chain")
        message = "Timed out reading policy from the chain." if isinstance(exc, TimeoutError) else f"Could not read policy from the chain: {exc}"
        raise HTTPException(status_code=502, detail={"error": "policy_unavailable", "message": message}) from exc

    snapshot, status = view.snapshot, view.status
    return PolicyResponse(
        owner=snapshot.owner,
        agents=snapshot.agents,
        token=snapshot.token,
        daily_cap=snapshot.daily_cap,
        per_call_max=snapshot.per_call_max,
        kill_switch_active=snapshot.kill_switch_active,
        allowlist=[AllowlistEntryOut(destination=e.destination, category=e.category) for e in snapshot.allowlist],
        spent_today=status.spent_today,
        remaining_today=status.remaining_today,
        period_start=datetime.fromtimestamp(status.period_start, tz=timezone.utc),
        fetched_at=view.fetched_at,
    )


async def _build(client: SorobanContractClient, settings: Settings, function_name: str, parameters: list, owner_public_key: str, description: str) -> UnsignedTransactionEnvelope:
    xdr = await client.build_owner_invocation_xdr(function_name, parameters, owner_public_key)
    return UnsignedTransactionEnvelope(xdr=xdr, network_passphrase=settings.network_passphrase, description=description)


@router.post("/daily-cap", response_model=UnsignedTransactionEnvelope, summary="Build an unsigned update_daily_cap transaction")
async def build_daily_cap_tx(
    payload: SetDailyCapRequest,
    client: SorobanContractClient = Depends(get_soroban_client),
    settings: Settings = Depends(get_app_settings),
) -> UnsignedTransactionEnvelope:
    return await _build(
        client, settings, CONTRACT_FN_UPDATE_DAILY_CAP, [scval.to_int128(int(payload.daily_cap))],
        payload.owner_public_key, f"Set daily cap to {payload.daily_cap}",
    )


@router.post("/per-call-max", response_model=UnsignedTransactionEnvelope, summary="Build an unsigned update_per_call_max transaction")
async def build_per_call_max_tx(
    payload: SetPerCallMaxRequest,
    client: SorobanContractClient = Depends(get_soroban_client),
    settings: Settings = Depends(get_app_settings),
) -> UnsignedTransactionEnvelope:
    return await _build(
        client, settings, CONTRACT_FN_UPDATE_PER_CALL_MAX, [scval.to_int128(int(payload.per_call_max))],
        payload.owner_public_key, f"Set per-call max to {payload.per_call_max}",
    )


@router.post("/allowlist/add", response_model=UnsignedTransactionEnvelope, summary="Build an unsigned add_allowlist_entry transaction")
async def build_add_allowlist_entry_tx(
    payload: AddAllowlistEntryRequest,
    client: SorobanContractClient = Depends(get_soroban_client),
    settings: Settings = Depends(get_app_settings),
) -> UnsignedTransactionEnvelope:
    return await _build(
        client, settings, CONTRACT_FN_ADD_ALLOWLIST_ENTRY,
        [scval.to_address(payload.destination), scval.to_symbol(payload.category)],
        payload.owner_public_key, f"Approve {payload.destination} ({payload.category})",
    )


@router.post("/allowlist/remove", response_model=UnsignedTransactionEnvelope, summary="Build an unsigned remove_allowlist_entry transaction")
async def build_remove_allowlist_entry_tx(
    payload: RemoveAllowlistEntryRequest,
    client: SorobanContractClient = Depends(get_soroban_client),
    settings: Settings = Depends(get_app_settings),
) -> UnsignedTransactionEnvelope:
    return await _build(
        client, settings, CONTRACT_FN_REMOVE_ALLOWLIST_ENTRY, [scval.to_address(payload.destination)],
        payload.owner_public_key, f"Revoke {payload.destination}",
    )


@router.post("/agents/add", response_model=UnsignedTransactionEnvelope, summary="Build an unsigned add_agent transaction")
async def build_add_agent_tx(
    payload: AddAgentRequest,
    client: SorobanContractClient = Depends(get_soroban_client),
    settings: Settings = Depends(get_app_settings),
) -> UnsignedTransactionEnvelope:
    return await _build(
        client, settings, CONTRACT_FN_ADD_AGENT, [scval.to_address(payload.agent)],
        payload.owner_public_key, f"Register agent {payload.agent}",
    )


@router.post(
    "/agents/remove",
    response_model=UnsignedTransactionEnvelope,
    summary="Build an unsigned remove_agent transaction",
    description="The contract itself refuses this if `agent` is the last remaining registered agent.",
)
async def build_remove_agent_tx(
    payload: RemoveAgentRequest,
    client: SorobanContractClient = Depends(get_soroban_client),
    settings: Settings = Depends(get_app_settings),
) -> UnsignedTransactionEnvelope:
    return await _build(
        client, settings, CONTRACT_FN_REMOVE_AGENT, [scval.to_address(payload.agent)],
        payload.owner_public_key, f"Deregister agent {payload.agent}",
    )


@router.post("/kill-switch", response_model=UnsignedTransactionEnvelope, summary="Build an unsigned set_kill_switch transaction")
async def build_kill_switch_tx(
    payload: KillSwitchRequest,
    client: SorobanContractClient = Depends(get_soroban_client),
    settings: Settings = Depends(get_app_settings),
) -> UnsignedTransactionEnvelope:
    return await _build(
        client, settings, CONTRACT_FN_SET_KILL_SWITCH, [scval.to_bool(payload.active)],
        payload.owner_public_key, "Activate kill switch" if payload.active else "Deactivate kill switch",
    )


@router.post(
    "/ownership/transfer",
    response_model=UnsignedTransactionEnvelope,
    summary="Build an unsigned transfer_ownership transaction",
    description="Step 1 of the two-step ownership transfer. Takes effect only once the new owner calls /policy/ownership/accept.",
)
async def build_transfer_ownership_tx(
    payload: TransferOwnershipRequest,
    client: SorobanContractClient = Depends(get_soroban_client),
    settings: Settings = Depends(get_app_settings),
) -> UnsignedTransactionEnvelope:
    return await _build(
        client, settings, CONTRACT_FN_TRANSFER_OWNERSHIP, [scval.to_address(payload.new_owner)],
        payload.owner_public_key, f"Propose ownership transfer to {payload.new_owner}",
    )


@router.post(
    "/ownership/accept",
    response_model=UnsignedTransactionEnvelope,
    summary="Build an unsigned accept_ownership transaction",
    description="Step 2 of the two-step ownership transfer — signed and submitted by the pending owner's own wallet.",
)
async def build_accept_ownership_tx(
    payload: AcceptOwnershipRequest,
    client: SorobanContractClient = Depends(get_soroban_client),
    settings: Settings = Depends(get_app_settings),
) -> UnsignedTransactionEnvelope:
    return await _build(
        client, settings, CONTRACT_FN_ACCEPT_OWNERSHIP, [], payload.pending_owner_public_key, "Accept ownership transfer",
    )
