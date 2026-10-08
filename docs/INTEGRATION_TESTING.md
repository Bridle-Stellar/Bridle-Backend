# Integration testing against Stellar testnet

The unit suite (`pytest`) never touches a network: Soroban RPC is faked
and decoders are pinned to recorded testnet XDR. This page covers the
other half, which is running the backend against a real Bridle Contract
on testnet. You can do that two ways:

1. **Automated, self-provisioning (recommended):** `tests/integration/`
   deploys its own fresh contract instance and drives this backend with
   the real SDK. It needs no keys or secrets.
2. **Manual:** point a running backend at a contract you control and call
   `client.pay()` yourself.

## 1. Automated: `tests/integration/`

### What it does

`tests/integration/test_testnet_end_to_end.py`, in order:

1. Generates four throwaway keypairs (owner, agent, merchant, relayer) and
   funds each with [Friendbot](https://friendbot.stellar.org).
2. Uploads the Bridle Contract wasm, creates a new contract instance, and
   calls `initialize` (owner, agent, native XLM as the governed token,
   100 XLM daily cap, 30 XLM per-call max).
3. Starts this backend under uvicorn on a free local port with a real
   `SorobanContractClient` (relayer key = the funded relayer) and an
   in-memory database.
4. Exercises it over real HTTP:
   - `GET /policy?fresh=true` matches what `initialize` set.
   - `POST /policy/allowlist/add` → signed by the owner → submitted; the
     allowlist then shows the merchant with category `compute`.
   - `BridleClient.pay()` 10 XLM → approved, both tx hashes present,
     `spent_today` moves on-chain, and the attempt is in `/transactions`.
   - The `spend_approved` event decodes with the right agent, destination,
     amount and token.
   - 30 XLM + 1 stroop → `per_call_max_exceeded`.
   - `POST /policy/kill-switch` on → `pay()` rejected with
     `kill_switch_active` → kill switch off again.

The tests build on each other's on-chain state, so run the file as a whole.

### Run it

You need the contract wasm. Build it from
[Bridle-Contract](https://github.com/Bridle-Stellar/Bridle-Contract):

```bash
# in a Bridle-Contract checkout
rustup target add wasm32v1-none
cargo build --target wasm32v1-none --release
# -> target/wasm32v1-none/release/bridle_contract.wasm
```

Then, from this repo (with `requirements.txt` and `sdk/` installed):

```bash
BRIDLE_INTEGRATION=1 \
BRIDLE_CONTRACT_WASM=../Bridle-Contract/target/wasm32v1-none/release/bridle_contract.wasm \
pytest tests/integration -v -s
```

PowerShell:

```powershell
$env:BRIDLE_INTEGRATION = "1"
$env:BRIDLE_CONTRACT_WASM = "..\Bridle-Contract\target\wasm32v1-none\release\bridle_contract.wasm"
pytest tests/integration -v -s
```

`-s` prints the new contract ID and accounts so you can look them up on
[stellar.expert](https://stellar.expert/explorer/testnet). A run takes
about a minute, mostly waiting for ledgers to close.

| Variable | Required | Default |
|---|---|---|
| `BRIDLE_INTEGRATION` | yes, must be `1` | — |
| `BRIDLE_CONTRACT_WASM` | yes | — |
| `BRIDLE_INTEGRATION_RPC_URL` | no | `https://soroban-testnet.stellar.org` |
| `BRIDLE_INTEGRATION_NETWORK_PASSPHRASE` | no | `Test SDF Network ; September 2015` |

Without both required variables the whole module is skipped, which is why
plain `pytest` and CI report those tests as skipped. The test is never faked:
it either reaches testnet and passes, or fails.

### When it fails

- **Friendbot errors / timeouts:** Friendbot is rate-limited and sometimes
  down. Re-run later.
- **`send_transaction failed` / `Transaction FAILED`:** usually an RPC
  hiccup or testnet reset in progress. If it repeats, the message carries
  the result XDR; decode it with `stellar xdr decode` or the
  [Stellar Lab](https://lab.stellar.org/xdr/view).
- **A decoder error (`... is missing field ...`):** the contract interface
  changed. Compare against Bridle-Contract's `docs/INTERFACE.md` and update
  `app/services/soroban_client.py` and `tests/fixtures/` together.

### Recorded run

| | |
|---|---|
| Date | 2026-10-08 |
| Wasm | Bridle-Contract `d7a560b`, sha256 `90650cce54be8dd8e969d414b04eb2b08cccf08708f7cc1e25fbdd6c116c7ee1` (the same wasm as the Contract's [DEPLOYMENTS.md](https://github.com/Bridle-Stellar/Bridle-Contract/blob/main/docs/DEPLOYMENTS.md)) |
| Contract created | [`CCIJXKUIWCT2T32SDKBH7LPQWJKANYWFSOCIZ2K7TNHBNBEVK2BOQAUP`](https://stellar.expert/explorer/testnet/contract/CCIJXKUIWCT2T32SDKBH7LPQWJKANYWFSOCIZ2K7TNHBNBEVK2BOQAUP) |
| Result | 6 passed in 59.5s (Python 3.14, stellar-sdk 16.1.0) |

Testnet is reset periodically, so that contract will eventually stop
resolving. That's expected, because every run deploys its own.

## 2. Manual: your own deployment

Use this to try the backend by hand, or to point the Frontend at it.

1. **Deploy a contract.** Follow the commands in Bridle-Contract's
   [`docs/DEPLOYMENTS.md`](https://github.com/Bridle-Stellar/Bridle-Contract/blob/main/docs/DEPLOYMENTS.md#commands)
   with your own `stellar keys` identities (owner, agent, merchant). The
   instance recorded there (`CAICNEKY…VUK5`) is fine to read with
   `GET /policy`. Paying through it needs its agent's key, which only that
   deployer has, so for `pay()` deploy your own.
2. **Create and fund a relayer account.** It only pays network fees and
   never authorizes a spend (see the README's "Auth model"):
   ```bash
   stellar keys generate bridle-relayer --network testnet --fund
   stellar keys show bridle-relayer   # prints the S... secret
   ```
3. **Configure and start the backend:**
   ```bash
   cp .env.example .env
   # BRIDLE_CONTRACT_ID=<your contract C...>
   # RELAYER_SECRET_KEY=<bridle-relayer secret>
   uvicorn app.main:app --reload
   ```
   Never commit `.env`. It's in `.gitignore`.
4. **Check the policy:** `curl "http://localhost:8000/policy?fresh=true"`.
   Your agent should be in `agents` and your merchant in `allowlist`.
5. **Pay as the agent:**
   ```python
   from bridle_sdk import BridleClient

   client = BridleClient(relay_url="http://localhost:8000", agent_secret_key="S...agent", timeout=60)
   print(client.pay(destination="G...merchant", amount="100000000", token="native"))
   ```
   The agent account itself holds the XLM being paid (Friendbot gives
   10,000), since the transfer's "from" is the agent.
