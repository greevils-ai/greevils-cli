# greevils-cli

The participant's CLI. **Everything the participant does runs through this** — encrypt,
submit, check status, deploy. The participant never touches the web. `encrypt` and `deploy`
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

### 1. Write your agent, then encrypt it
```bash
greevils encrypt my_agent.py -o agent.py.enc      # prints AGENT_KEY=...  — SAVE IT
```
`AGENT_KEY` is symmetric: it encrypts now and decrypts inside the TEE at deploy. **Save it
safely** — without it you can't deploy or redeploy, and it's never recoverable from the
backend (the backend only ever holds the ciphertext).

### 2. Submit the ciphertext (names your agent, stores it, triggers the build)
```bash
greevils submit agent.py.enc --name my-cool-agent
# submitted: id=a1b2c3d4  name=my-cool-agent  status=QUEUED
# token:     <secret>   (saved to ~/.greevils/tokens.json — keep a copy)
```
Submit returns a **submission token** — your write capability for that submission (it gates
reporting the deploy IP). The CLI saves it to `~/.greevils/tokens.json` and reuses it
automatically; the server keeps only its hash, so it can't be recovered if you lose it.

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
  --project your-gcp-project --zone us-central1-a
```
`deploy` resolves `image_ref`/`digest` from the submission id, then launches a Confidential
Space TDX VM running that exact image, passing `AGENT_KEY` + `MASTER_ACCOUNT` as
`tee-env-*`. It prints the VM's external IP (give it + the digest to the organizer/verifier).
`AGENT_KEY` / `MASTER_ACCOUNT` also read from `$AGENT_KEY` / `$MASTER_ACCOUNT`.

When you deploy by `<id>`, the CLI also **reports the VM's public IP back to the API**, which
marks the submission `DEPLOYED` (visible in `list`/`status`). The report is authenticated with
your **submission token** (saved on `submit`); the CLI finds it automatically, or pass
`--token` / set `$GREEVILS_TOKEN`. If the report fails, the deploy still succeeds and the CLI
prints a `curl` you can run to report the IP yourself.

> Deploy itself bypasses the API by design (`--id` is only used to look up the public image
> ref + digest, and to report the IP afterward). You can skip the API entirely with
> `greevils deploy --image-ref ... --digest ...` (no IP is reported in that case).

## Commands
| Command | What it does | Talks to API? |
|---|---|---|
| `encrypt <agent.py>` | generate `AGENT_KEY`, write ciphertext | no (local) |
| `submit <enc> --name N` | upload ciphertext, start the build | yes |
| `list` | list all submissions | yes |
| `status <id> [--log]` | one submission's status + digest + IP | yes |
| `deploy <id> …` | launch the CS TDX VM at the published digest, then report its IP | resolve digest + report IP |

## Layout
```
pyproject.toml          packaging + the `greevils` console-script entry point
greevils_cli/cli.py     argparse commands (encrypt/submit/list/status/deploy) + main()
greevils_cli/crypto.py  agent encryption (Fernet) — same scheme the TEE harness decrypts with
greevils_cli/deploy.py  launch the CS TDX VM at a digest (gcloud, fully local)
```
