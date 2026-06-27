# Writing a greevils agent

Your agent is a normal program that runs inside a Confidential Space TEE, in its own sandboxed
process with **no trading key**. It places trades by making plain HTTP calls to the harness's
loopback API — there is no SDK to import, and no language requirement beyond "the TEE runs
`python entry.py`". This directory is a complete, working example; copy it and replace `entry.py`
with your own logic.

## The contract

- **`entry.py` is required** — it's the fixed entrypoint. The TEE runs `python entry.py` and that's
  your agent. If it exits, your agent stops trading (the harness stays up so you can still
  withdraw), so most agents loop forever.
- **`requirements.txt` is optional** — your pip dependencies, installed inside the TEE before
  `entry.py` runs. (Deps are fetched at runtime and are *not* part of the image digest; pin
  versions/hashes if you need reproducibility.)
- **Structure your code however you like.** Multiple files, packages, whatever — the whole directory
  is zipped into your bundle. Build/VCS junk (`__pycache__`, `.git`, `*.pyc`, …) is excluded
  automatically.
- **Your secrets** (exchange API keys, etc.) are passed at deploy time, not baked into the bundle —
  see [Secrets](#secrets-agent_env) below. They arrive as normal environment variables.

## The trading API

The harness exposes a loopback HTTP API. Its URL is in the env var **`GREEVILS_AGENT_URL`**
(e.g. `http://127.0.0.1:8081`). All bodies are JSON:

| Method & path | Body | Returns |
|---|---|---|
| `POST /call` | `{"method": "<exchange method>", "params": {...}}` | `{"ok", "result", "error"}` |
| `GET /state` | — | `{"account_value", "withdrawable", "positions"}` |
| `GET /policy` | — | `{"allowed_coins": {coin: max_leverage}, "min_trading_balance"}` |
| `GET /address` | — | `{"address"}` (the TEE trading account) |

`POST /call` proxies a [Hyperliquid Python SDK `Exchange`](https://github.com/hyperliquid-dex/hyperliquid-python-sdk)
method: `method` is the method name and `params` are its keyword arguments. The harness holds the
key and signs for you; you never see it. Example — place a limit order:

```python
import json, os, urllib.request

URL = os.environ["GREEVILS_AGENT_URL"]

def call(method, **params):
    body = json.dumps({"method": method, "params": params}).encode()
    req = urllib.request.Request(URL + "/call", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        j = json.loads(r.read())
    return j["ok"], j["result"], j["error"]

ok, result, error = call(
    "order", name="BTC", is_buy=True, sz=0.001, limit_px=50000,
    order_type={"limit": {"tif": "Gtc"}},
)
```

See [`entry.py`](entry.py) for a fuller client (~12 lines of stdlib) and a complete trading loop.

### What you're allowed to call

The harness enforces a whitelist regardless of what your code tries (anything else returns
`ok=false` with a reason):

- **Methods:** `order`, `bulk_orders`, `market_open`, `market_close`, `modify_order`,
  `bulk_modify_orders_new`, `cancel`, `cancel_by_cloid`, `bulk_cancel`, `bulk_cancel_by_cloid`,
  `schedule_cancel`, `update_leverage`.
- **Coins & leverage:** only the coins in `GET /policy`, each up to its max leverage. At the time of
  writing: **BTC ≤ 10×, TAO ≤ 5×, ZEC ≤ 10×** — but read `/policy` at runtime rather than
  hard-coding.
- A small **builder fee** is attached to every order automatically; you don't set it.

### Market data

Reading prices/candles needs no key — query Hyperliquid directly (the example uses
`hyperliquid.info.Info`). The TEE has outbound network access (to PyPI, Hyperliquid, anywhere).

## Secrets (`AGENT_ENV`)

Don't put API keys in your bundle. Pass them at deploy time with `--env-file secrets.env`; the
harness injects them into your process's environment inside the TEE (the organizer never sees
them). In `entry.py` just read `os.environ["MY_API_KEY"]`. Names that collide with reserved harness
variables (e.g. `GREEVILS_AGENT_URL`, `AGENT_KEY`) are ignored.

## Logs

Anything your agent prints to stdout/stderr is captured and viewable on the agent's web UI at
`http://<vm-ip>:8080` (and via the authenticated `GET /logs`). Use `print(..., flush=True)` or rely
on unbuffered output — print freely; it's how you debug a running agent.

## Ship it

From the repo, with the `greevils` CLI installed:

```bash
# 1. Package + encrypt your agent directory. Prints AGENT_KEY (SAVE IT) and AGENT_SHA256.
greevils package ./my-agent -o agent-bundle.enc

# 2. Upload the ciphertext. Prints a submission id + token (saved locally for you).
greevils submit agent-bundle.enc --name my-agent

# 3. Wait for the image build. Poll until status is PUBLISHED.
greevils status <id>

# 4. Deploy into a Confidential Space TEE VM. Reuse the AGENT_KEY from step 1.
greevils deploy <id> --agent-key <AGENT_KEY> --master-account 0xYourAddress \
                     --env-file secrets.env        # optional: your API keys etc.

# 5. Poll until status is RUNNING. Confirm agent_hash == the AGENT_SHA256 from step 1 --
#    that proves the TEE is running exactly the code you packaged.
greevils status <id>
```

`--master-account` is the address your funds return to when you wind the agent down. Keep your
`AGENT_KEY` safe: it's the only thing that can decrypt your bundle, and you need it to redeploy.
