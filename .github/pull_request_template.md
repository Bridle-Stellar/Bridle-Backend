## What and why

<!-- What does this change, and why? Link the issue: Closes #N -->

## How it was tested

<!-- New tests, and anything you ran by hand (e.g. the testnet integration test, curl calls). -->

## Checklist

- [ ] `ruff check .` passes
- [ ] `pytest` passes, and new behavior has a test
- [ ] No policy rule added here that the contract doesn't enforce; `check_and_record_spend` is still always called before a transfer
- [ ] Only `app/services/soroban_client.py` talks to Soroban RPC
- [ ] Chain-read failures surface as errors, never as default values
- [ ] Decoders and `tests/fixtures/` updated together if the contract interface changed
- [ ] If the relay flow, signing, or decoders changed: ran `tests/integration` against testnet
- [ ] README Endpoints / `app/schemas.py` descriptions updated if an endpoint changed
- [ ] No secrets, keys, or `.env` files in the diff
