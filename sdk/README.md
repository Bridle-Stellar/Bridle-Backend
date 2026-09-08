# bridle-sdk

Minimal Python client for routing an AI agent's payments through a
Bridle relay so they're checked against on-chain spending guardrails.

## Install

From the repo root (editable, for local development against this backend):

```bash
pip install -e sdk/
```

## Usage

```python
from bridle_sdk import BridleClient, BridleRejected

client = BridleClient(relay_url="https://your-bridle-instance", agent_secret_key="S...")

try:
    result = client.pay(destination="GDESTINATION...", amount="1000000", token="native")
    print("paid:", result.payment_tx_hash)
except BridleRejected as e:
    print("blocked:", e.reason, e.message)  # e.g. "daily_cap_exceeded"
```

An `async def` agent loop can use `await client.pay_async(...)` instead.

`agent_secret_key` is the Stellar secret key for the agent identity
registered with Bridle Contract (via its owner-run `add_agent`) — **not**
Bridle Backend's own relayer key. Bridle Contract requires the agent
itself to authorize every spend, so `client.pay(...)` signs two Soroban
authorization entries locally with this key before sending anything to
the relay; the key itself is never transmitted. See the backend README's
"Auth model" section if you want the full why.

That's the whole surface for v1: point your existing payment call at
`BridleClient.pay()` instead of calling the destination directly, and
handle `BridleRejected` the way you'd handle any other payment decline.
