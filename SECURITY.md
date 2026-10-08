# Security Policy

This backend sits in the payment path of an autonomous AI agent: it
builds the authorization entries the agent signs, submits them with its
own relayer key, and tells the dashboard what the on-chain policy is. A
bug here can lead an agent to sign something it didn't intend, make a
spend that should have been refused look approved (or the reverse), or
show an owner a kill switch as "off" when it's on. **Security reports
are welcome and are prioritized over all other work.**

## Reporting a vulnerability

Report privately through GitHub:

**[Report a vulnerability](https://github.com/Bridle-Stellar/Bridle-Backend/security/advisories/new)**

Only the maintainers can see these reports. Please don't open a public
issue, PR, or discussion for a suspected vulnerability until it has been
fixed.

Include whatever you have:

- what an attacker can do, and what they need first (network position,
  the relayer key, an agent key, access to the API);
- the affected endpoint, module, and commit;
- steps or a test that reproduces it. A failing test under `tests/` is
  ideal; `tests/conftest.py` already builds and signs real authorization
  entries.

## What to expect

- Acknowledgement within 3 days.
- An initial assessment (confirmed, needs more info, or not an issue)
  within 7 days.
- For confirmed issues, we'll agree a disclosure timeline with you, fix it,
  and credit you in the advisory unless you'd rather stay anonymous.

This is a small open-source project with no bug bounty.

## Scope

In scope, roughly in order of how much we care:

- **The relayer trust boundary.** The relayer key may only pay fees and
  submit entries the agent already signed. Anything that lets the
  relayer (or someone holding its key) authorize a spend, alter what was
  signed, or move funds on its own is critical. See the README's
  "Auth model".
- **The signing flow** (`/relay/prepare` → agent signs → `/relay/submit`).
  This covers getting an agent to sign an entry for a different
  destination, token, amount or contract than it declared; submitting a
  signed entry that doesn't match the request (see
  `_validate_spend_invocation` / `_validate_transfer_invocation` in
  `app/routers/relay.py`); replaying or reusing a signed entry; and
  problems with the entry-validity window (`AUTH_ENTRY_VALIDITY_LEDGERS`).
- **Sending a transfer without an approved `check_and_record_spend`**,
  or treating a rejected or failed spend as approved.
- **Misreporting policy.** `GET /policy`, `/transactions/summary` or the
  decoders in `app/services/soroban_client.py` returning a default,
  partial, or stale value as if it were current, especially for the
  kill switch.
- **Relayer key exposure** through logs, error messages, API responses,
  or config handling.
- **The policy write proxy** (`/policy/*`) building a transaction that
  does something other than what its `description` says.
- **The SDK** (`sdk/bridle_sdk`) leaking the agent's secret key or
  signing something other than what it was asked to pay.

Out of scope:

- The Soroban contract's own rules. Report those in
  [Bridle-Contract](https://github.com/Bridle-Stellar/Bridle-Contract/security/advisories/new).
- Bridle Frontend. Report that in its own repo.
- Bugs in stellar-sdk, Soroban RPC, or Stellar itself. Report those
  upstream.
- The `/transactions/*` read endpoints having no auth or rate limiting.
  That's a known gap, tracked as a public issue.
- The documented design choice that the relayer decides which spends to
  *ask* the agent to sign.
- Throwaway testnet keys and balances, including ones the integration
  test creates.

## Supported versions

Only `main` is supported. There is no mainnet deployment.
