"""
Thin wrapper around stellar-sdk's Soroban RPC client — the only module in
this backend allowed to talk to Soroban RPC. Everything else (routers,
policy_service, sync_worker) goes through the `SorobanContractClient`
defined here.

--- Contract interface: verified ---
Function names, return types, event topics and event data fields match
Bridle-Contract's docs/INTERFACE.md at Contract commit d7a560b. The ScVal
decoders at the bottom of this module (`_parse_policy_snapshot`,
`_parse_spend_status`, `_parse_spend_outcome`, `_decode_event_value`) are
tested against real XDR read back from that contract's testnet deployment
(see tests/fixtures/testnet_interface.py), not hand-built values. They are
strict: an unexpected shape raises SorobanCallError rather than defaulting.
If the contract's `#[contracttype]`s or events change, update INTERFACE.md
there and the decoders and fixtures here together.

--- Auth model (confirmed) ---
`check_and_record_spend` requires the *agent's* Soroban authorization
(`agent.require_auth()`), not the relayer's, and the same is true of the
SEP-41 `transfer` the relayer submits afterward (its "from" is the agent).
This backend never holds a key that can authorize either call — it can
only submit transactions carrying authorization entries the agent has
already signed with its own key. See app/routers/relay.py for how those
entries are obtained (a two-step prepare/submit exchange, not a single
call) and this repo's README, "Auth model" section, for the full
rationale carried over from the contract's README.
"""
from __future__ import annotations

from dataclasses import dataclass

from stellar_sdk import Account, Address, Asset, Keypair, TransactionBuilder, scval
from stellar_sdk import xdr as stellar_xdr
from stellar_sdk.exceptions import PrepareTransactionException
from stellar_sdk.soroban_rpc import EventFilter, EventFilterType
from stellar_sdk.soroban_server_async import SorobanServerAsync

from app.config import Settings

# --- Confirmed contract function names -------------------------------------------
CONTRACT_FN_INITIALIZE = "initialize"
CONTRACT_FN_CHECK_AND_RECORD_SPEND = "check_and_record_spend"
CONTRACT_FN_GET_POLICY = "get_policy"
CONTRACT_FN_GET_SPEND_STATUS = "get_spend_status"
CONTRACT_FN_UPDATE_DAILY_CAP = "update_daily_cap"
CONTRACT_FN_UPDATE_PER_CALL_MAX = "update_per_call_max"
CONTRACT_FN_ADD_ALLOWLIST_ENTRY = "add_allowlist_entry"
CONTRACT_FN_REMOVE_ALLOWLIST_ENTRY = "remove_allowlist_entry"
CONTRACT_FN_ADD_AGENT = "add_agent"
CONTRACT_FN_REMOVE_AGENT = "remove_agent"
CONTRACT_FN_SET_KILL_SWITCH = "set_kill_switch"
CONTRACT_FN_TRANSFER_OWNERSHIP = "transfer_ownership"
CONTRACT_FN_ACCEPT_OWNERSHIP = "accept_ownership"

TOKEN_FN_TRANSFER = "transfer"  # standard SEP-41 interface, not Bridle-specific

# --- Confirmed contract event names (topic[0], snake_case) -----------------------
EVENT_SPEND_APPROVED = "spend_approved"
EVENT_SPEND_REJECTED = "spend_rejected"
EVENT_DAILY_CAP_UPDATED = "daily_cap_updated"
EVENT_PER_CALL_MAX_UPDATED = "per_call_max_updated"
EVENT_KILL_SWITCH_TOGGLED = "kill_switch_toggled"
EVENT_ALLOWLIST_ENTRY_ADDED = "allowlist_entry_added"
EVENT_ALLOWLIST_ENTRY_REMOVED = "allowlist_entry_removed"
EVENT_AGENT_ADDED = "agent_added"
EVENT_AGENT_REMOVED = "agent_removed"
EVENT_OWNERSHIP_TRANSFER_PROPOSED = "ownership_transfer_proposed"
EVENT_OWNERSHIP_TRANSFERRED = "ownership_transferred"

# --- Contract RejectReason variants (docs/INTERFACE.md, "RejectReason") ----------
CONTRACT_REJECT_REASONS = (
    "KillSwitchActive",
    "UnknownAgent",
    "InvalidAmount",
    "WrongToken",
    "DestinationNotAllowed",
    "ExceedsPerCallMax",
    "ExceedsDailyCap",
)

DEFAULT_BASE_FEE = 100
TX_TIMEOUT_SECONDS = 30


class SorobanCallError(RuntimeError):
    """Raised when a Soroban RPC call fails or returns something this client can't parse."""


def resolve_token_contract_id(token: str, network_passphrase: str) -> str:
    """"native" means XLM via its well-known Stellar Asset Contract; anything
    else is already a SEP-41 token contract ID. Centralized here so every
    caller (relay flow, payment building, the SDK's own /relay/prepare
    request) treats "native" identically."""
    if token == "native":
        return Asset.native().contract_id(network_passphrase)
    return token


@dataclass(frozen=True)
class AllowlistEntry:
    destination: str
    category: str


@dataclass(frozen=True)
class PolicySnapshot:
    """Raw `get_policy()` result."""

    owner: str
    agents: list[str]
    token: str
    daily_cap: int
    per_call_max: int
    kill_switch_active: bool
    allowlist: list[AllowlistEntry]


@dataclass(frozen=True)
class SpendStatus:
    """Raw `get_spend_status()` result."""

    daily_cap: int
    period_start: int  # 00:00:00 UTC of the current day, unix seconds
    spent_today: int
    remaining_today: int  # signed: negative if the cap was lowered below today's spend


@dataclass(frozen=True)
class PolicyState:
    """Merged view used by the fast local pre-check (app/services/policy_service.py).
    Combines one get_policy() read and one get_spend_status() read."""

    allowlist: list[str]  # destinations only; category isn't needed for the pre-check
    per_call_max: int
    daily_cap: int
    spent_today: int
    kill_switch_active: bool
    token: str


@dataclass(frozen=True)
class PreparedAuthorization:
    """An unsigned authorization entry plus the ledger it's valid until,
    ready for the named party to sign with stellar_sdk.auth.authorize_entry()."""

    entry_xdr: str
    valid_until_ledger: int


@dataclass(frozen=True)
class DecodedInvocation:
    """What a signed (or unsigned) SorobanAuthorizationEntry actually
    commits to — used to cross-check a request's plaintext fields against
    what was really signed before this backend acts on it."""

    contract_id: str
    function_name: str
    args: list
    authorizer: str | None


@dataclass(frozen=True)
class SpendAuthorization:
    """Result of submitting check_and_record_spend — the final policy authority."""

    authorized: bool
    tx_hash: str
    reason: str | None = None  # populated when authorized is False
    spent_today: int | None = None
    remaining_today: int | None = None


@dataclass(frozen=True)
class RawContractEvent:
    ledger: int
    event_type: str
    topic: str
    data: dict
    tx_hash: str


class SorobanContractClient:
    """Async client for the single Bridle Contract instance this backend guards."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._server = SorobanServerAsync(settings.soroban_rpc_url)
        self._contract_id = settings.bridle_contract_id
        self._relayer_keypair = (
            Keypair.from_secret(settings.relayer_secret_key) if settings.relayer_secret_key else None
        )

    async def close(self) -> None:
        await self._server.close()

    # -- Reads --------------------------------------------------------------------

    async def get_policy_snapshot(self) -> PolicySnapshot:
        result = await self._simulate_read(CONTRACT_FN_GET_POLICY, [])
        return _parse_policy_snapshot(result)

    async def get_spend_status(self) -> SpendStatus:
        result = await self._simulate_read(CONTRACT_FN_GET_SPEND_STATUS, [])
        return _parse_spend_status(result)

    async def get_policy_state(self) -> PolicyState:
        """Convenience read for the local pre-check: one get_policy() call
        and one get_spend_status() call, merged into the shape
        policy_service.py needs."""
        snapshot = await self.get_policy_snapshot()
        status = await self.get_spend_status()
        return PolicyState(
            allowlist=[e.destination for e in snapshot.allowlist],
            per_call_max=snapshot.per_call_max,
            daily_cap=snapshot.daily_cap,
            spent_today=status.spent_today,
            kill_switch_active=snapshot.kill_switch_active,
            token=snapshot.token,
        )

    async def latest_ledger(self) -> int:
        health = await self._server.get_latest_ledger()
        return health.sequence

    async def fetch_new_events(self, start_ledger: int, limit: int = 100) -> list[RawContractEvent]:
        response = await self._server.get_events(
            start_ledger=start_ledger,
            filters=[EventFilter(event_type=EventFilterType.CONTRACT, contract_ids=[self._contract_id])],
            limit=limit,
        )
        return [decode_raw_event(e.ledger, e.transaction_hash, e.topic, e.value) for e in response.events]

    # -- Agent-authorized spend flow -----------------------------------------------
    # See app/routers/relay.py for the two-step exchange these support: the
    # agent signs what prepare_* returns, and submit_* attaches that signed
    # entry to a fresh, relayer-submitted transaction.

    async def prepare_check_and_record_spend(
        self, agent: str, destination: str, token_contract_id: str, amount: int
    ) -> PreparedAuthorization:
        return await self._prepare_authorization(
            contract_id=self._contract_id,
            function_name=CONTRACT_FN_CHECK_AND_RECORD_SPEND,
            parameters=[
                scval.to_address(agent),
                scval.to_address(destination),
                scval.to_address(token_contract_id),
                scval.to_int128(amount),
            ],
            authorizer=agent,
        )

    async def prepare_transfer(
        self, agent: str, destination: str, token_contract_id: str, amount: int
    ) -> PreparedAuthorization:
        return await self._prepare_authorization(
            contract_id=token_contract_id,
            function_name=TOKEN_FN_TRANSFER,
            parameters=[scval.to_address(agent), scval.to_address(destination), scval.to_int128(amount)],
            authorizer=agent,
        )

    async def submit_check_and_record_spend(self, signed_entry_xdr: str) -> SpendAuthorization:
        """The final-authority call. Attaches the agent's already-signed
        entry to a fresh transaction and submits it; never re-derives or
        re-signs the invocation itself."""
        if self._relayer_keypair is None:
            raise SorobanCallError("relayer_secret_key is not configured; cannot submit contract transactions.")

        send_result = await self._submit_signed_entry(signed_entry_xdr)
        if send_result is None:
            return SpendAuthorization(authorized=False, tx_hash="", reason="Simulation rejected the authorization entry.")

        tx_hash, get_result = send_result
        if get_result.status.value != "SUCCESS":
            return SpendAuthorization(authorized=False, tx_hash=tx_hash, reason=f"Transaction {get_result.status.value.lower()}.")

        outcome = _parse_spend_outcome(get_result.result_meta_xdr)
        if outcome.approved:
            return SpendAuthorization(
                authorized=True, tx_hash=tx_hash,
                spent_today=outcome.spent_today, remaining_today=outcome.remaining_today,
            )
        return SpendAuthorization(authorized=False, tx_hash=tx_hash, reason=f"Rejected by the policy contract: {outcome.reason}")

    async def submit_transfer(self, signed_entry_xdr: str) -> str:
        """Submits the SEP-41 transfer, agent-authorized, that Bridle
        Contract's README describes the relayer performing once
        check_and_record_spend returns Approved. Raises SorobanCallError on
        any failure — callers should treat that as "authorized on-chain but
        undelivered", not as a policy rejection."""
        if self._relayer_keypair is None:
            raise SorobanCallError("relayer_secret_key is not configured; cannot submit contract transactions.")

        send_result = await self._submit_signed_entry(signed_entry_xdr)
        if send_result is None:
            raise SorobanCallError("Transfer simulation rejected the authorization entry.")
        tx_hash, get_result = send_result
        if get_result.status.value != "SUCCESS":
            raise SorobanCallError(f"Transfer transaction {get_result.status.value.lower()}.")
        return tx_hash

    # -- Owner-signed policy writes -------------------------------------------------
    # Unlike the agent-authorized spend flow above, these are built with the
    # OWNER as the transaction's source account. A wallet like Freighter
    # signs the assembled transaction directly — which also satisfies the
    # contract's owner.require_auth() for an address matching the connected
    # wallet — so no separate entry-signing round trip is needed here.

    async def build_owner_invocation_xdr(self, function_name: str, parameters: list, owner_public_key: str) -> str:
        source_account = await self._server.load_account(owner_public_key)
        tx = (
            TransactionBuilder(source_account, self._settings.network_passphrase, base_fee=DEFAULT_BASE_FEE)
            .append_invoke_contract_function_op(
                contract_id=self._contract_id, function_name=function_name, parameters=parameters
            )
            .set_timeout(TX_TIMEOUT_SECONDS)
            .build()
        )
        prepared = await self._server.prepare_transaction(tx)
        return prepared.to_xdr()

    # -- internals ------------------------------------------------------------------

    async def _simulate_read(self, function_name: str, parameters: list) -> stellar_xdr.SCVal:
        source = self._relayer_keypair or Keypair.random()
        source_account = Account(source.public_key, sequence=0)
        tx = (
            TransactionBuilder(source_account, self._settings.network_passphrase, base_fee=DEFAULT_BASE_FEE)
            .append_invoke_contract_function_op(
                contract_id=self._contract_id, function_name=function_name, parameters=parameters
            )
            .set_timeout(TX_TIMEOUT_SECONDS)
            .build()
        )
        sim = await self._server.simulate_transaction(tx)
        if sim.error or not sim.results:
            raise SorobanCallError(f"{function_name} simulation failed: {sim.error}")
        return stellar_xdr.SCVal.from_xdr(sim.results[0].xdr)

    async def _prepare_authorization(
        self, contract_id: str, function_name: str, parameters: list, authorizer: str
    ) -> PreparedAuthorization:
        source = self._relayer_keypair or Keypair.random()
        source_account = Account(source.public_key, sequence=0)
        tx = (
            TransactionBuilder(source_account, self._settings.network_passphrase, base_fee=DEFAULT_BASE_FEE)
            .append_invoke_contract_function_op(contract_id=contract_id, function_name=function_name, parameters=parameters)
            .set_timeout(TX_TIMEOUT_SECONDS)
            .build()
        )
        sim = await self._server.simulate_transaction(tx)
        if sim.error or not sim.results:
            raise SorobanCallError(f"{function_name} simulation failed: {sim.error}")

        entry_xdr = _find_unsigned_entry_for(sim.results[0].auth or [], authorizer)
        if entry_xdr is None:
            raise SorobanCallError(
                f"Simulating {function_name} did not request an authorization entry for {authorizer}; "
                "check that this address is passed as the argument the contract calls require_auth() on."
            )
        valid_until_ledger = sim.latest_ledger + self._settings.auth_entry_validity_ledgers
        return PreparedAuthorization(entry_xdr=entry_xdr, valid_until_ledger=valid_until_ledger)

    async def _submit_signed_entry(self, signed_entry_xdr: str):
        entry = stellar_xdr.SorobanAuthorizationEntry.from_xdr(signed_entry_xdr)
        invoked = entry.root_invocation.function.contract_fn
        contract_id = Address.from_xdr_sc_address(invoked.contract_address).address
        function_name = invoked.function_name.sc_symbol.decode()

        source_account = await self._server.load_account(self._relayer_keypair.public_key)
        tx = (
            TransactionBuilder(source_account, self._settings.network_passphrase, base_fee=DEFAULT_BASE_FEE)
            .append_invoke_contract_function_op(
                contract_id=contract_id, function_name=function_name, parameters=list(invoked.args), auth=[entry]
            )
            .set_timeout(TX_TIMEOUT_SECONDS)
            .build()
        )
        try:
            prepared = await self._server.prepare_transaction(tx)
        except PrepareTransactionException:
            return None
        prepared.sign(self._relayer_keypair)
        send_result = await self._server.send_transaction(prepared)
        get_result = await self._server.poll_transaction(send_result.hash)
        return send_result.hash, get_result


def decode_invocation(entry_xdr: str) -> DecodedInvocation:
    """Decodes what a (signed or unsigned) authorization entry actually
    commits to. Used by the relay router to verify a request's plaintext
    fields match what was really signed, before this backend acts on it."""
    entry = stellar_xdr.SorobanAuthorizationEntry.from_xdr(entry_xdr)
    function = entry.root_invocation.function
    if function.type != stellar_xdr.SorobanAuthorizedFunctionType.SOROBAN_AUTHORIZED_FUNCTION_TYPE_CONTRACT_FN:
        raise SorobanCallError("Only direct contract-function authorizations are supported.")
    invoked = function.contract_fn
    return DecodedInvocation(
        contract_id=Address.from_xdr_sc_address(invoked.contract_address).address,
        function_name=invoked.function_name.sc_symbol.decode(),
        args=[_plain_native(scval.to_native(a)) for a in invoked.args],
        authorizer=_credential_address(entry.credentials),
    )


def _plain_native(value):
    return value.address if isinstance(value, Address) else value


def _credential_address(credentials: stellar_xdr.SorobanCredentials) -> str | None:
    if credentials.type == stellar_xdr.SorobanCredentialsType.SOROBAN_CREDENTIALS_ADDRESS:
        return Address.from_xdr_sc_address(credentials.address.address).address
    if credentials.type == stellar_xdr.SorobanCredentialsType.SOROBAN_CREDENTIALS_ADDRESS_V2:
        return Address.from_xdr_sc_address(credentials.address_v2.address).address
    return None


def _find_unsigned_entry_for(entries: list[str], authorizer: str) -> str | None:
    for entry_xdr in entries:
        decoded = decode_invocation(entry_xdr)
        if decoded.authorizer == authorizer:
            return entry_xdr
    return None


def decode_raw_event(ledger: int, tx_hash: str, topic_xdr_list: list, value_xdr) -> RawContractEvent:
    """One getEvents entry (base64 topic/value XDR) -> RawContractEvent."""
    return RawContractEvent(
        ledger=ledger,
        event_type=_decode_topic0(topic_xdr_list),
        topic=_decode_topic(topic_xdr_list),
        data=_decode_event_value(value_xdr),
        tx_hash=tx_hash,
    )


def _decode_topic0(topic_xdr_list: list) -> str:
    if not topic_xdr_list:
        return ""
    return str(_json_native(scval.to_native(topic_xdr_list[0])))


def _decode_topic(topic_xdr_list: list) -> str:
    """`<event name>.<topic 1>.<topic 2>...` with addresses as plain G.../C...
    strings, e.g. `spend_approved.<agent>.<destination>`."""
    return ".".join(str(_json_native(scval.to_native(t))) for t in topic_xdr_list)


def _decode_event_value(value_xdr) -> dict:
    """Every Bridle event's data is an ScMap of its non-topic fields
    (docs/INTERFACE.md, "Events"); fields-less events carry an empty map,
    not Void. Returned JSON-safe so it can be stored as-is."""
    native = _json_native(scval.to_native(value_xdr))
    if not isinstance(native, dict):
        raise SorobanCallError(f"Expected an event data map, got {native!r}")
    return native


def _json_native(value):
    """scval.to_native output with Address objects flattened to their
    G.../C... strings, recursively, so the result is JSON-serializable."""
    if isinstance(value, Address):
        return value.address
    if isinstance(value, dict):
        return {str(k): _json_native(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_native(v) for v in value]
    return value


# --- ScVal decoding for get_policy / get_spend_status / SpendOutcome -------------
# Verified against Bridle-Contract's docs/INTERFACE.md (Contract commit
# d7a560b) and against real values read back from its testnet deployment
# (contract CAICNEKY4YT7M2K56RTI47WBFAUE2KZRKHZIYPFPQWGB23DDERZJVUK5); the
# XDR in tests/fixtures/testnet_interface.py is that real data.
#
# Strict on purpose: a missing or wrongly-typed field raises SorobanCallError
# instead of falling back to a default. A defaulted kill switch reads as
# "off", which is the unsafe direction.


def _field(native: dict, key: str, expected: type, type_name: str):
    if key not in native:
        raise SorobanCallError(f"{type_name} is missing field {key!r}; got keys {sorted(native)}")
    value = native[key]
    if expected is str and isinstance(value, Address):
        return value.address
    # bool is a subclass of int; never accept one for the other.
    if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
        raise SorobanCallError(f"{type_name}.{key} should be {expected.__name__}, got {value!r}")
    return value


def _as_map(native, type_name: str) -> dict:
    if not isinstance(native, dict):
        raise SorobanCallError(f"Expected {type_name} to decode to a map, got {native!r}")
    return native


def _parse_policy_snapshot(value: stellar_xdr.SCVal) -> PolicySnapshot:
    native = _as_map(scval.to_native(value), "PolicySnapshot")
    allowlist = []
    for raw in _field(native, "allowlist", list, "PolicySnapshot"):
        entry = _as_map(raw, "AllowlistEntry")
        allowlist.append(
            AllowlistEntry(
                destination=_field(entry, "destination", str, "AllowlistEntry"),
                category=_field(entry, "category", str, "AllowlistEntry"),
            )
        )
    agents = _field(native, "agents", list, "PolicySnapshot")
    for agent in agents:
        if not isinstance(agent, Address):
            raise SorobanCallError(f"PolicySnapshot.agents should hold addresses, got {agent!r}")
    return PolicySnapshot(
        owner=_field(native, "owner", str, "PolicySnapshot"),
        agents=[a.address for a in agents],
        token=_field(native, "token", str, "PolicySnapshot"),
        daily_cap=_field(native, "daily_cap", int, "PolicySnapshot"),
        per_call_max=_field(native, "per_call_max", int, "PolicySnapshot"),
        kill_switch_active=_field(native, "kill_switch", bool, "PolicySnapshot"),
        allowlist=allowlist,
    )


def _parse_spend_status(value: stellar_xdr.SCVal) -> SpendStatus:
    native = _as_map(scval.to_native(value), "SpendStatus")
    return SpendStatus(
        daily_cap=_field(native, "daily_cap", int, "SpendStatus"),
        period_start=_field(native, "period_start", int, "SpendStatus"),
        spent_today=_field(native, "spent_today", int, "SpendStatus"),
        remaining_today=_field(native, "remaining_today", int, "SpendStatus"),
    )


@dataclass(frozen=True)
class _SpendOutcome:
    approved: bool
    reason: str | None = None  # RejectReason variant name, e.g. "ExceedsPerCallMax"
    spent_today: int | None = None
    remaining_today: int | None = None


def _parse_spend_outcome(result_meta_xdr: str | None) -> _SpendOutcome:
    if not result_meta_xdr:
        raise SorobanCallError("Transaction succeeded but returned no result metadata.")
    meta = stellar_xdr.TransactionMeta.from_xdr(result_meta_xdr)
    # Protocol 23+ (testnet is on 29) returns TransactionMeta v4; v3 is kept
    # for older networks. Both carry the contract's return value in soroban_meta.
    body = meta.v4 or meta.v3
    if body is None or body.soroban_meta is None or body.soroban_meta.return_value is None:
        raise SorobanCallError(f"Transaction result metadata (v{meta.v}) did not contain a Soroban return value.")
    return parse_spend_outcome_value(body.soroban_meta.return_value)


def parse_spend_outcome_value(value: stellar_xdr.SCVal) -> _SpendOutcome:
    """`SpendOutcome` is a tuple-variant union: Vec[Symbol("Approved"),
    SpendReceipt map] or Vec[Symbol("Rejected"), Vec[Symbol(<RejectReason>)]]."""
    native = scval.to_native(value)
    if not (isinstance(native, list) and len(native) == 2 and isinstance(native[0], str)):
        raise SorobanCallError(f"Unrecognized SpendOutcome encoding: {native!r}")
    tag, payload = native

    if tag == "Approved":
        receipt = _as_map(payload, "SpendReceipt")
        return _SpendOutcome(
            approved=True,
            spent_today=_field(receipt, "spent_today", int, "SpendReceipt"),
            remaining_today=_field(receipt, "remaining_today", int, "SpendReceipt"),
        )
    if tag == "Rejected":
        return _SpendOutcome(approved=False, reason=parse_reject_reason(payload))
    raise SorobanCallError(f"Unrecognized SpendOutcome variant: {tag!r}")


def parse_reject_reason(native) -> str:
    """A `RejectReason` is a unit-variant enum, which decodes to a
    one-element list holding the variant name: ["ExceedsPerCallMax"]."""
    if isinstance(native, list) and len(native) == 1 and native[0] in CONTRACT_REJECT_REASONS:
        return native[0]
    raise SorobanCallError(f"Unrecognized RejectReason encoding: {native!r}")
