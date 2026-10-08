# Contributing to Bridle Backend

Thanks for helping. This service sits in an AI agent's payment path, so
changes are held to a high bar for tests and clarity. Small, focused PRs
get reviewed fastest.

## Setup

You need Python 3.11 or newer.

```bash
python -m venv .venv
source .venv/bin/activate        # .venv\Scripts\activate on Windows
pip install -r requirements.txt  # app + pytest + ruff
pip install -e sdk/              # the client SDK, used by the tests
```

You don't need a Stellar account, a deployed contract, or a `.env` to run
the tests. To run the backend against testnet, see the README's "Setup"
and [docs/INTEGRATION_TESTING.md](docs/INTEGRATION_TESTING.md).

## Test and lint

These are the same checks CI runs on Python 3.11 and 3.12 for every PR.
Run them before you push:

```bash
ruff check .        # add --fix for import order and similar
pytest
```

The unit tests never touch a network. If your change touches how this
backend talks to the chain (the relay flow, the decoders, the write
proxy), also run the opt-in testnet integration test and say in the PR
that you did:

```bash
BRIDLE_INTEGRATION=1 BRIDLE_CONTRACT_WASM=path/to/bridle_contract.wasm pytest tests/integration -v -s
```

## Module boundaries

The layout is deliberately narrow so one issue shouldn't need the whole
system in your head. Keep it that way:

- **No policy logic here.** The Soroban contract is the only authority;
  its `check_and_record_spend` decides every spend. Never add a rule to
  this backend that the contract doesn't enforce, and never skip the
  on-chain call.
- **`app/services/soroban_client.py` is the only module that talks to
  Soroban RPC.** Routers, the sync worker and everything else go through
  `SorobanContractClient`. Contract function names, event names and ScVal
  decoding live there and nowhere else.
- **Decoders stay strict.** They match Bridle-Contract's
  [`docs/INTERFACE.md`](https://github.com/Bridle-Stellar/Bridle-Contract/blob/main/docs/INTERFACE.md).
  An unexpected shape raises `SorobanCallError`, never a default. If the
  contract interface changes, update the decoders and
  `tests/fixtures/testnet_interface.py` in the same PR, using real values
  read from a deployment, not hand-built ones.
- **`app/services/policy_service.py` is pure.** The local pre-check takes
  a policy snapshot and returns a result: no I/O, no clock, no cache.
- **Routers get dependencies via `app/deps.py`** (`Depends(...)`), never
  by reaching into `app.state`, so tests can override them.
- **Read endpoints only read.** Endpoints like `GET /policy` serialize
  what the contract returns. If a chain read fails they return an error
  (502), never a fallback value.
- **The relayer key never authorizes anything.** It pays fees and submits
  entries the agent already signed. Don't add a code path where it signs
  a spend, a transfer, or a policy change.
- **Amounts are integers in the token's smallest unit** on the way in and
  out of the chain.

## Picking up an issue

1. Look for issues labelled
   [`good first issue`](https://github.com/Bridle-Stellar/Bridle-Backend/labels/good%20first%20issue)
   or with a `complexity:` label that matches your experience. Each issue
   lists the background it needs.
2. Comment on the issue saying you'd like to take it, with a sentence or
   two on your approach. Wait for a maintainer to assign it to you before
   starting, so two people don't build the same thing.
3. If you go quiet for 7 days with no PR or update, the issue may be
   unassigned so someone else can pick it up. Just say so if you need
   more time.

For anything that changes API behavior and isn't already an issue, open
an issue first. Docs fixes and typos can go straight to a PR.

## Branches and commits

- Branch from `main`. Name it `<type>/<short-description>`, e.g.
  `feat/transactions-csv`, `fix/sync-cursor-gap`, `docs/relay-diagram`.
- Write commit messages in the
  [Conventional Commits](https://www.conventionalcommits.org/) style:
  `feat:`, `fix:`, `docs:`, `test:`, `ci:`, `refactor:`, `chore:`.
- One logical change per PR. If you find an unrelated problem along the
  way, open a separate issue or PR for it.

## Pull requests

- Fill in the PR template, and link the issue with `Closes #N`.
- New behavior needs tests. Name each test after the behavior it
  guarantees, e.g. `test_chain_failure_returns_502_not_a_default_policy`.
- Update the README's Endpoints section if you add or change an endpoint;
  the OpenAPI docs come from `app/schemas.py`, so keep field descriptions
  accurate there too.
- Never commit `.env`, a secret key (`S...`), or a contract ID or
  transaction hash you didn't actually create or observe.
- CI must be green. A maintainer reviews every PR; changes to the relay
  flow, signing, or the decoders need extra review time.

## Security issues

Don't open a public issue for a vulnerability. See [SECURITY.md](SECURITY.md).

## Code of Conduct

Everyone taking part is expected to follow the
[Code of Conduct](CODE_OF_CONDUCT.md).
