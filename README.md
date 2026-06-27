# greevils-cli

The participant's CLI. **Everything the participant does runs through this** — package,
submit, check status, deploy. The participant never touches the web. `package` and `deploy`
are fully local (the backend never sees plaintext or your `AGENT_KEY`); `submit` / `list` /
`status` talk to [greevils-api](../greevils-api/).

## Install
```bash
cd greevils-cli
python3 -m venv .venv && . .venv/bin/activate
pip install -e .        # installs a real `greevils` command on PATH (no alias needed)
```
The `greevils` command comes from the console-script entry point in
[pyproject.toml](pyproject.toml). Use `pipx install .` to install it globally instead.

Point it at the backend with `--api` or `GREEVILS_API` (default `http://localhost:8000`).

## The full workflow

### 1. Build your agent directory, then package it
Your agent is a **directory** that MUST contain `entry.py` (the fixed entrypoint, run as
`python entry.py` inside the TEE) and SHOULD contain `requirements.txt` (your pip deps). Lay out
the rest however you like. Your code trades by making plain HTTP calls to the harness at
`$GREEVILS_AGENT_URL` (`http://127.0.0.1:8081`) — no SDK to import. See
[example-agent/](example-agent/).
```bash
greevils package ./my-agent -o agent-bundle.enc   # prints AGENT_KEY=... and AGENT_SHA256=...
```
`AGENT_KEY` is symmetric: it encrypts now and decrypts inside the TEE at deploy. **Save it
safely** — without it you can't deploy or redeploy, and it's never recoverable from the backend
(the backend only ever holds the ciphertext). `AGENT_SHA256` is the agent identity the harness
publishes at `GET /agent`, so you can confirm exactly which code is running.

### 2. Submit the bundle (names your agent, stores it, triggers the build)
```bash
greevils submit agent-bundle.enc --name my-cool-agent
# submitted: id=a1b2c3d4  name=my-cool-agent  status=QUEUED
# token:     <secret>   (saved to ~/.greevils/tokens.json — keep a copy)
```
Submit returns a **submission token** — your write capability for that submission (it gates
reporting the deploy IP). The CLI saves it to `~/.greevils/tokens.json` and reuses it
automatically; the server keeps only its hash, so it can't be recovered if you lose it.

**Pip packages.** Put a `requirements.txt` **inside your agent directory** (so it's encrypted in
the bundle — the organizer never sees it). The agent process pip-installs it at runtime inside
the TEE, before `entry.py` runs. Because it's installed from PyPI at runtime, it is **not** part
of digest **D** (only the requirements.txt text is, via the agent hash); pin versions/hashes if
you need reproducible installs, and prefer wheels (the slim base has no compiler).

### 3. Watch the build
```bash
greevils list                 # all submissions; find yours by name
greevils status a1b2c3d4      # QUEUED -> BUILDING_IMAGE -> PUBLISHING_IMAGE -> PUBLISHED
greevils status a1b2c3d4 --log    # include the build log (handy on FAILED)
```
When `PUBLISHED`, `status` shows the `image_ref` and `image_digest` (D).

### 4. Deploy with the published image
```bash
greevils deploy a1b2c3d4 \
  --agent-key "$AGENT_KEY" \
  --master-account 0xYourEOA \
  --env-file .env \
  --project your-gcp-project --zone us-central1-a
```
`deploy` resolves `image_ref`/`digest` from the submission id, then launches a Confidential
Space TDX VM running that exact image, passing `AGENT_KEY` + `MASTER_ACCOUNT` as
`tee-env-*`. It prints the VM's external IP (give it + the digest to the organizer/verifier).
`AGENT_KEY` / `MASTER_ACCOUNT` also read from `$AGENT_KEY` / `$MASTER_ACCOUNT`.

**Your own env vars / API keys (`--env-file`).** Put any env vars your agent needs in a
`.env`-style file (`KEY=VALUE` per line; `#` comments and a leading `export ` are fine) and
pass `--env-file .env`. Your agent then reads them with `os.environ["OPENAI_API_KEY"]` etc.,
as usual. The CLI packs the whole file into one base64(JSON) blob and passes it as a single
`tee-env-AGENT_ENV`, which the TEE harness unpacks into the environment before your agent
loads. Because deploy runs **on your own infra**, these names and values live only in your own
VM's metadata — the organizer never sees them, and they are **not** part of the attested
digest. A handful of names are reserved and silently ignored if present in your file:
`AGENT_KEY`, `MASTER_ACCOUNT`, `AGENT_ENV`, `CS_TOKEN_BACKEND`, `CS_TOKEN_AUDIENCE`.

When you deploy by `<id>`, the CLI also **reports the VM's public IP back to the API**, which
marks the submission `DEPLOYED` (visible in `list`/`status`). The report is authenticated with
your **submission token** (saved on `submit`); the CLI finds it automatically, or pass
`--token` / set `$GREEVILS_TOKEN`. If the report fails, the deploy still succeeds and the CLI
prints a `curl` you can run to report the IP yourself.

> Deploy itself bypasses the API by design (`--id` is only used to look up the public image
> ref + digest, and to report the IP afterward). You can skip the API entirely with
> `greevils deploy --image-ref ... --digest ...` (no IP is reported in that case).

### 5. Claim your Hyperliquid account on-chain (subnet miners)

To be scored by the subnet validator, register your neuron on the subnet, then publish a
one-time on-chain **ownership commitment** that proves you control a Hyperliquid account
(either a greevil **agent** account or a **normal** trading account):

Sign the canonical message in the Greevils web UI with your Hyperliquid account, then pass
the resulting address + signature to the CLI to commit it (the CLI never sees your key):

```bash
greevils commit --netuid 1 --coldkey my-wallet --hotkey my-hotkey \
  --hl-address 0xACCT --signature 0xSIG
```

The on-chain commitment is the compact blob `base64(hl_address(20B) ‖ signature(65B))`
(116 chars) written via Bittensor's `set_commitment` — the Raw commitment field is capped at
128 bytes, so the signed message itself is **not** stored. `signature` is an EIP-191
`personal_sign` over the *canonical message* (`Greevils Hyperliquid ownership claim` + your
hotkey + the account address) by the Hyperliquid account's key. The validator rebuilds that
message from your committing **hotkey** + the embedded address and checks the signature
recovers to the address — so nobody can copy your blob and claim the same account (it would
rebuild under their hotkey and fail). Use `--dry-run` to build + self-verify the commitment
and print it without writing on-chain. That's the miner's whole job — once committed, the
validator evaluates you every round.

> `commit` talks to the **subtensor chain**, not the greevils-api. It needs the `bittensor`
> packages (already in this CLI's dependencies).

### 6. Approve agent image digests (validators)

A validator decides which agent image digests are eligible for **agent** rewards. Put the full
approved set in a JSON file (an array of digest strings) and publish it for your hotkey:

```bash
cat approved.json        # ["sha256:abc...", "sha256:def..."]
greevils approve approved.json
```

This does two things: (1) POSTs the canonical list + an **sr25519 signature** (proving you own
the hotkey) to greevils-api, which stores it under your hotkey; and (2) commits the list's hash
on-chain as `gva1:<base64(sha256(list))>`. Validators take the **highest-staked validator-permit
holder**, fetch its list from greevils-api, and use it only if the hash matches its on-chain
commitment — so the off-chain host can't tamper with the list. The file is the **full** approved
set and replaces your previous one; `--dry-run` skips the chain write. Anyone may publish, but
only the top validator's commitment is honoured.

## Commands
| Command | What it does | Talks to API? |
|---|---|---|
| `package <dir>` | zip + encrypt an agent dir (needs `entry.py`) → `agent-bundle.enc` + `AGENT_KEY` + `AGENT_SHA256` | no (local) |
| `submit <enc> --name N` | upload the encrypted bundle, start the build | yes |
| `list` | list all submissions | yes |
| `status <id> [--log]` | one submission's status + digest + IP | yes |
| `deploy <id> …` | launch the CS TDX VM at the published digest, then report its IP | resolve digest + report IP |
| `commit …` | claim a Hyperliquid account on-chain (the miner's job) | no (subtensor chain) |
| `approve <file.json>` | publish your hotkey's approved agent digests (validators) | yes + subtensor chain |

## Layout
```
pyproject.toml          packaging + the `greevils` console-script entry point
greevils_cli/cli.py     argparse commands (package/submit/list/status/deploy) + main()
greevils_cli/crypto.py  agent bundle zip + encryption (Fernet) — same scheme the TEE harness decrypts with
greevils_cli/deploy.py  launch the CS TDX VM at a digest (gcloud, fully local)
greevils_cli/commit.py  build/sign/verify the on-chain Hyperliquid ownership commitment
greevils_cli/approve.py canonicalize/hash the approved-digest list + build its commitment
```
