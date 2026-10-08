# Bridle Backend

[![CI](https://github.com/Bridle-Stellar/Bridle-Backend/actions/workflows/ci.yml/badge.svg)](https://github.com/Bridle-Stellar/Bridle-Backend/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Off-chain relayer/middleware for **Bridle**: lets a human set spending
guardrails for an autonomous AI agent's crypto wallet on Stellar. This
repo sits between an AI agent's payment client and the destination
service, checks every payment attempt against on-chain policy (allowlist,
caps, kill switch) held by a separate Soroban contract ("Bridle
Contract"), and only forwards approved payments. It also ships a minimal
client SDK (`sdk/`) so wrapping an agent's existing payment calls is close
to a one-line change.

**This backend contains no policy logic of its own.** The Soroban
contract is the sole source of truth; its `check_and_record_spend` call is
the final authority on every request. The local pre-check
(`app/services/policy_service.py`) is a latency optimization, never a
substitute — see Bridle Contract's own README for the actual enforcement
rules this backend mirrors for that pre-check.

**Verified contract interface.** Function names, return-value encodings
(`PolicySnapshot`, `SpendStatus`, `SpendOutcome`, `RejectReason`) and
event topics/data fields match Bridle-Contract's
[`docs/INTERFACE.md`](https://github.com/Bridle-Stellar/Bridle-Contract/blob/main/docs/INTERFACE.md)
at Contract commit `d7a560b`. The decoders in
`app/services/soroban_client.py` are tested against real XDR read back
from that contract's testnet deployment
(`CAICNEKY…VUK5`, see its
[`docs/DEPLOYMENTS.md`](https://github.com/Bridle-Stellar/Bridle-Contract/blob/main/docs/DEPLOYMENTS.md)):
`get_policy` and `get_spend_status` results, the `TransactionMeta` v4 of
one approved and one rejected `check_and_record_spend`, and the emitted
events. The fixtures are in `tests/fixtures/testnet_interface.py`. The
decoders are strict: an unexpected shape is an error, never a default.

## Auth model

**Confirmed from Bridle Contract's README:** the *agent's own key*
authorizes every spend, via Soroban's native authorization framework —
not the relayer's. `check_and_record_spend` calls `agent.require_auth()`,
and the SEP-41 transfer the relayer submits afterward requires the same
from the agent as its "from". A signed Soroban authorization entry is
cryptographic proof the agent itself requested precisely this
destination/token/amount; the relayer can choose which spend to *ask* the
agent to sign and can submit the result, but cannot fabricate, alter, or
reuse one.

This backend's relayer key (`RELAYER_SECRET_KEY`) reflects that directly:

- It **can**: be the transaction's source account — paying the network
  fee and providing the sequence number — for a transaction carrying an
  authorization entry the agent has already signed.
- It **cannot**: authorize a spend or a transfer on its own. It is never
  the "agent" argument to `check_and_record_spend` and never the "from" of
  a transfer. A compromised relayer key can waste this account's XLM on
  fees; it cannot move funds or approve spends by itself.
- It also **cannot** change policy (cap, allowlist, kill switch, agents,
  ownership) — those require the contract owner's own wallet signature
  (see "Policy write proxy" below).

The agent's own secret key lives only in the agent's own process, via the
client SDK (`sdk/bridle_sdk`) — it is never sent to this backend. See
"The relay flow" below for exactly what *is* sent instead.

## The relay flow

Because the agent — not the relayer — must sign each invocation, a plain
"POST the payment, get paid" endpoint isn't possible: the agent has to
sign something before this backend can submit anything on its behalf.
The relay is a two-step exchange instead of a single call, and the
client SDK (`sdk/bridle_sdk`) hides both steps behind one `client.pay()`:

```
Agent's client (via bridle_sdk)                    Bridle Backend
        │                                                 │
        │  POST /relay/prepare {destination,amount,token} │
        ├────────────────────────────────────────────────▶│  local pre-check (fast reject if obviously doomed)
        │                                                  │  simulate check_and_record_spend + transfer on Soroban
        │◀────────────────────────────────────────────────┤  → two UNSIGNED authorization entries
        │                                                  │
        │  sign both entries locally with the agent's key  │
        │  (stellar_sdk.auth.authorize_entry — no network  │
        │   call needed to sign)                           │
        │                                                  │
        │  POST /relay/submit {..., signed entries}        │
        ├────────────────────────────────────────────────▶│  decode + cross-check entries against the request
        │                                                  │  re-run local pre-check
        │                                                  │  submit check_and_record_spend (final authority)
        │                                                  │  if Approved: submit the SEP-41 transfer
        │◀────────────────────────────────────────────────┤  → approved (with tx hashes) or a structured rejection
        │                                                  │  every attempt logged either way
```

This is deliberately **not** the literal x402 "402 challenge, resend with
an X-PAYMENT header" handshake — Stellar's Soroban authorization framework
already provides the primitive x402 emulates for other chains (a
recipient-agnostic, argument-bound signed credential), so this backend
uses that directly instead of layering x402's header conventions on top
of it. The two-call shape (declare → get back something to sign → sign →
resubmit) is the same idea in spirit.

## Architecture

```
Agent's client (bridle_sdk)
        │  /relay/prepare, /relay/submit
        ▼
┌─────────────────────────── Bridle Backend ────────────────────────────┐
│  1. Local pre-check (app/services/policy_service.py)                  │
│     — fast rejection using a short-TTL cached policy snapshot         │
│  2. check_and_record_spend on Soroban (app/services/soroban_client)   │
│     — the final authority; always called, never skipped               │
│  3. SEP-41 transfer, agent-authorized (app/services/soroban_client)   │
│     — only reached after step 2 returns Approved                     │
│  Every attempt logged to the local DB regardless of outcome           │
└─────────────────────────────────────────────────────────────────────┘
        │                                   ▲
        ▼                                   │ background poll
  Destination account              Sync worker (app/services/sync_worker.py)
                                    backfills spends made outside the relay

Bridle Frontend (dashboard) ── reads only ──▶ /transactions/*, GET /policy (this repo)
                             ── policy changes ──▶ /policy/* → unsigned XDR
                                                    → signed by owner's wallet (e.g. Freighter)
                                                    → submitted directly to the network
```

## Tech stack

- Python 3.11+, FastAPI, fully async (this sits in a payment hot path)
- `stellar-sdk` for all Soroban RPC / transaction / authorization-entry work
- SQLite by default via SQLAlchemy's async engine (`aiosqlite`) — swapping
  to Postgres is a `DATABASE_URL` change, nothing else (see `app/database.py`)

## Setup

```bash
python -m venv .venv
source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
pip install -e sdk/          # optional, for local SDK development

cp .env.example .env
# edit .env: at minimum set BRIDLE_CONTRACT_ID and RELAYER_SECRET_KEY

uvicorn app.main:app --reload
```

Then open http://127.0.0.1:8000/docs for interactive OpenAPI docs.

The database schema is created automatically on startup
(`app/database.py:init_db`) — fine for SQLite/dev. For Postgres in
production, switch to Alembic migrations instead of relying on
`create_all`.

## Configuration

See `.env.example` for the full list with descriptions. Everything is an
environment variable — nothing chain- or network-specific is hardcoded.
Notable ones:

| Variable | Purpose |
|---|---|
| `BRIDLE_CONTRACT_ID` | The Bridle Contract instance this backend guards (one per deployment, v1) |
| `RELAYER_SECRET_KEY` | This backend's fee-paying signing key — see "Auth model" |
| `SOROBAN_RPC_URL` / `NETWORK_PASSPHRASE` | Must point at the same network |
| `DATABASE_URL` | SQLite by default; any SQLAlchemy async URL works |
| `AUTH_ENTRY_VALIDITY_LEDGERS` | How long a prepared authorization entry stays valid for the agent to sign and return (~5 min default) |
| `SYNC_POLL_INTERVAL_SECONDS` | How often the sync worker polls for new chain events |
| `POLICY_CACHE_TTL_SECONDS` | How long a policy snapshot is reused before re-reading the chain for the local pre-check |

## Running the sync worker

It starts automatically with the app (`SYNC_WORKER_ENABLED=true`, the
default) as a background asyncio task — no separate process to run. It
polls `getEvents` on the Soroban RPC endpoint every
`SYNC_POLL_INTERVAL_SECONDS` (default 5s), starting from a persisted
ledger cursor (`sync_cursor` table), and backfills any spend that
happened on-chain without going through `/relay/submit` (e.g. manual CLI
testing against the contract).

**Tuning:** lower the interval for a shorter window where an out-of-band
spend is missing from the dashboard, at the cost of more RPC calls;
raise it if you're hitting RPC rate limits. 5s is a reasonable starting
point for testnet development. Disable it entirely (`SYNC_WORKER_ENABLED=false`)
for tests or single-shot scripts — this is also what the test suite does.

## Endpoints

Full request/response schemas are in `/docs` (OpenAPI, auto-generated
from `app/schemas.py`). Payments require signing, so they can't be
demonstrated with plain curl — use the SDK (see below) or the equivalent
snippet here. Everything else is a normal curl-able REST call.

### `POST /relay/prepare` + `POST /relay/submit` — the core relay

```python
from bridle_sdk import BridleClient, BridleRejected

client = BridleClient(relay_url="http://localhost:8000", agent_secret_key="S...")
try:
    result = client.pay(destination="GDEST...", amount="1000000", token="native")
    print(result.payment_tx_hash)
except BridleRejected as e:
    print(e.reason, e.message)  # e.g. "daily_cap_exceeded"
```

Approved (`/relay/submit` → 200):
```json
{"status": "approved", "transaction_id": "...", "payment_tx_hash": "...", "contract_tx_hash": "...", "spent_today": 1000000, "remaining_today": 9000000}
```

Rejected (403) — `rejection.reason` is one of `destination_not_allowlisted`,
`per_call_max_exceeded`, `daily_cap_exceeded`, `kill_switch_active`,
`chain_authorization_denied`, `upstream_error`:
```json
{"detail": {"status": "rejected", "transaction_id": "...", "rejection": {"reason": "daily_cap_exceeded", "message": "..."}}}
```

### Transaction log (read-only, for the dashboard)

```bash
curl "http://localhost:8000/transactions?status=rejected&limit=20"
curl "http://localhost:8000/transactions/summary"
curl "http://localhost:8000/transactions/stats?bucket=day"
curl "http://localhost:8000/transactions/{id}"
```

### `GET /policy` — current on-chain policy (read-only)

The contract's own `get_policy()` and `get_spend_status()`, serialized
as-is for the dashboard: owner, registered agents, governed token, caps,
kill switch, the full allowlist with categories, and today's spend.
Amounts are JSON integers in the token's smallest unit (stroops for XLM).

```bash
curl "http://localhost:8000/policy"
curl "http://localhost:8000/policy?fresh=true"   # bypass the cache
```

```json
{
  "owner": "GD4IDHGFDQAOSFCXLXAK5FDNI5GHKLDZJEEC6Z7O5J5IMP3G5ATJ6OX4",
  "agents": ["GCPV6J54IAAW7YKYF23KOEVGIHXEU5UH4LRCB3KTPECGXLPCDWGXLL5M"],
  "token": "CDLZFC3SYJYDZT7K67VZ75HPJVIEUVNIXF47ZG2FB2RMQQVU2HHGCYSC",
  "daily_cap": 10000000000,
  "per_call_max": 3000000000,
  "kill_switch_active": false,
  "allowlist": [{"destination": "GD57XJTA237YNXWO6N3PAJKLZPIDKV77OJK7OCCFHVOZAOC6WQJ5OMDP", "category": "compute"}],
  "spent_today": 500000000,
  "remaining_today": 9500000000,
  "period_start": "2026-10-08T00:00:00Z",
  "fetched_at": "2026-10-08T15:30:00Z"
}
```

(Values are from the Contract's testnet deployment; see
`tests/fixtures/testnet_interface.py`.)

- **Caching.** By default this is served from the same short-TTL cache as
  the relay pre-check (`POLICY_CACHE_TTL_SECONDS`, default 2s);
  `fetched_at` says when the chain was actually read. Pass `?fresh=true`
  to read the chain now. Do that before acting on `kill_switch_active`,
  since a cached reading can be up to one TTL old.
- **Failure.** If the chain can't be read, or returns something the strict
  decoder doesn't recognize, the response is `502` and there is no policy
  in the body:
  `{"detail": {"error": "policy_unavailable", "message": "..."}}`. It never
  falls back to a default, and `?fresh=true` never falls back to a cached
  value.
- `remaining_today` can be negative if the owner lowered `daily_cap` below
  what was already spent today.

### Policy write proxy

The frontend never talks to the Soroban contract directly for policy
changes. It POSTs the intended change here, gets back an **unsigned**
transaction built with the owner as its source account, and has the
owner's wallet (e.g. Freighter) sign and submit it — which satisfies both
the transaction's own signature and the contract's `owner.require_auth()`
in one step, since Freighter signs any authorization entries bound to the
connected address as part of signing the transaction. This keeps all
contract-invocation logic in one place instead of duplicated in the
frontend.

```bash
curl -X POST http://localhost:8000/policy/daily-cap \
  -H "Content-Type: application/json" \
  -d '{"daily_cap": "50000000", "owner_public_key": "GOWNER..."}'
# → {"xdr": "AAAA...", "network_passphrase": "...", "description": "Set daily cap to 50000000"}
```

Same pattern for the rest of the owner-only contract calls, one endpoint
each: `POST /policy/per-call-max`, `POST /policy/allowlist/add`
(`{destination, category, owner_public_key}`), `POST /policy/allowlist/remove`
(`{destination, owner_public_key}`), `POST /policy/agents/add` /
`POST /policy/agents/remove` (`{agent, owner_public_key}`),
`POST /policy/kill-switch` (`{active, owner_public_key}`), and the
two-step `POST /policy/ownership/transfer` (`{new_owner, owner_public_key}`)
/ `POST /policy/ownership/accept` (`{pending_owner_public_key}`).

## Using the client SDK

```python
from bridle_sdk import BridleClient, BridleRejected

client = BridleClient(relay_url="http://localhost:8000", agent_secret_key="S...")
try:
    result = client.pay(destination="GDEST...", amount="1000000", token="native")
except BridleRejected as e:
    print(e.reason, e.message)
```

`agent_secret_key` is the Stellar secret key for the agent identity
registered with Bridle Contract — see "Auth model" above for why the SDK
needs it and what it does with it (signs locally, never transmits it).
See `sdk/README.md` for the async variant and install instructions.

## Testing

```bash
pip install -r requirements.txt   # includes pytest, pytest-asyncio
pip install -e sdk/
pytest
ruff check .                      # same lint CI runs
```

CI (`.github/workflows/ci.yml`) runs both on Python 3.11 and 3.12 for every
push to `main` and every pull request.

- `tests/test_policy_service.py` — unit tests for the local pre-check,
  covering the same scenarios expected of the contract's own test suite
  (over cap, over per-call max, not allowlisted, kill switch on), with
  Soroban RPC fully mocked out.
- `tests/test_relay_api.py` — integration-style tests of the full
  `/relay/prepare` → sign → `/relay/submit` flow. Signing uses real
  `stellar_sdk` crypto against real, correctly-shaped unsigned
  authorization entries (`tests/conftest.py:FakeSorobanClient`), so the
  actual decode/validate/sign code paths run for real — only the network
  submission itself is faked. Covers approval, each rejection reason, and
  a tampered-amount request being caught before it reaches the chain.
- `tests/test_policy_api.py` — `GET /policy` from real testnet XDR to
  JSON, cache hit vs `?fresh=true`, kill switch on, allowlist
  serialization, and 502 on chain failure/timeout (never a default policy).
- `tests/test_soroban_parsers.py` — the contract-return and event decoders
  against real testnet XDR (`tests/fixtures/`), plus the malformed shapes
  they must refuse (e.g. a missing `kill_switch` is an error, not "off").
- `tests/test_sync_worker.py` — sync worker ingestion of real
  `spend_approved` / `spend_rejected` / policy events, including the
  contract `RejectReason` → API `rejection_reason` mapping and dedupe.
- `tests/test_sdk_client.py` — unit test for the SDK's own signing step.
- `tests/test_transactions_api.py` — filtering, pagination, and the
  summary/stats aggregate calculations against a seeded in-memory SQLite DB.

**Manual/integration testing against real testnet:** once a Bridle
Contract instance is deployed to testnet, point `.env` at its
`BRIDLE_CONTRACT_ID`, fund `RELAYER_SECRET_KEY` with testnet XLM via
[Friendbot](https://friendbot.stellar.org) (it only needs enough for
fees), register an agent identity with the contract's `add_agent` and
fund *that* keypair too if the token is native XLM held in the agent's
own account, and run `client.pay(...)` via the SDK end to end. There's no
automated CI integration test against a live testnet yet (would need a
way to stand up a fresh contract instance per run) — that's a good first
contribution.

**Latency note:** this sits in a payment hot path, and it now makes more
RPC round trips than a single-call design would: `/relay/prepare` does a
policy read (usually cache-served) plus two simulate calls; `/relay/submit`
does another policy read plus two submit-and-poll cycles (one for
check_and_record_spend, one for the transfer). On testnet, a single RPC
round trip typically runs 200–800ms and a submit-and-poll cycle (submit +
wait for the ledger to close) is closer to 1–6s, so a full `pay()` call —
both HTTP round trips combined — should typically land in the
low-to-mid single-digit seconds. This is a direct consequence of the
confirmed auth model (two agent-signed, sequentially-submitted on-chain
calls) rather than something this backend can optimize away; treat calls
well past `REQUEST_TIMEOUT_SECONDS` (default 8s, per HTTP call) as an RPC
latency signal to investigate, not a reason to raise it.

## Project layout

```
app/
  main.py            FastAPI app, lifespan wiring (DB, Soroban client, sync worker)
  config.py          Environment-variable settings
  database.py        Async SQLAlchemy engine/session setup
  models.py          ORM models (Transaction, ContractEvent, SyncCursor)
  schemas.py         Pydantic request/response models (also the OpenAPI schema)
  deps.py            FastAPI dependency providers
  routers/
    relay.py         POST /relay/prepare, POST /relay/submit
    transactions.py  GET /transactions, /transactions/summary, /transactions/stats
    policy.py        GET /policy, POST /policy/* (owner-only contract calls, see above)
  services/
    soroban_client.py   All Soroban RPC interaction lives here — see its module docstring
    policy_service.py   Pure, testable local pre-check logic
    policy_cache.py     Short-TTL cache in front of policy reads (pre-check and GET /policy)
    sync_worker.py      Background polling loop
sdk/
  bridle_sdk/        Minimal client SDK package
tests/
```

## Non-goals (v1)

- No policy enforcement logic duplicated here — see the top of this file.
- No UI — that's Bridle Frontend; this repo only exposes APIs.
- No multiple concurrent contracts/agents-per-request-shape assumptions
  beyond what the contract itself supports — `BRIDLE_CONTRACT_ID` is a
  single value per deployment, and while the contract supports multiple
  registered agents (this backend accepts `agent_public_key` per request
  and logs it), there's no per-agent policy here — policy is global,
  matching the contract.

## Contributing

Module boundaries are kept deliberately narrow so a single well-scoped
issue shouldn't require understanding the whole system. Some starter
issues:

- Add a new rejection reason category (`app/models.py:RejectionReason` →
  handle it in `policy_service.py` → document it in this README's
  endpoint table).
- Add CSV export to `GET /transactions` (an `?format=csv` query param).
- Add a real integration test against a locally-run Soroban testnet
  contract instance in CI.
- Swap the `sync_cursor` single-row bookmark for per-contract cursors
  ahead of multi-contract support.

## License

MIT, see [LICENSE](LICENSE).
