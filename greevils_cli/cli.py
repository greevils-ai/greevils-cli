#!/usr/bin/env python3
"""greevils -- the participant CLI. Everything the participant does runs through here.

  greevils encrypt agent.py [-o agent.py.enc]      # generate AGENT_KEY + write ciphertext
  greevils submit agent.py.enc --name my-agent     # upload ciphertext to greevils-api
  greevils list                                    # all submissions (spot your own by name)
  greevils status <id>                             # one submission's status + image digest
  greevils deploy <id> --agent-key K --master-account 0x...   # launch the CS TDX VM
  greevils commit --hl-address 0x... --signature 0x...        # claim a Hyperliquid account on-chain
  greevils verify --hl-address 0x... --signature 0x...        # locally verify a commitment (no on-chain write)
  greevils approve approved.json                              # publish your hotkey's approved agent digests (JSON array file)

encrypt + deploy are fully local (the API never sees plaintext or your key). submit/list/
status just talk to the greevils-api backend (--api or GREEVILS_API, default https://api.greevils.ai).
commit writes a Bittensor on-chain commitment that the validator reads -- it talks to the
subtensor chain, not the API.
"""
import argparse
import json
import os
import sys
from pathlib import Path

import requests

from . import __version__
from .crypto import encrypt_agent, package_dir
from .deploy import deploy

DEFAULT_API = os.environ.get("GREEVILS_API", "https://api.greevils.ai")

# Per-submission write tokens live here, keyed by submission id. Saved on `submit`, used
# automatically by `deploy`. Override the dir with GREEVILS_HOME.
TOKEN_STORE = Path(os.environ.get("GREEVILS_HOME", str(Path.home() / ".greevils"))) / "tokens.json"


# ---- submission-token store -----------------------------------------------

def _save_token(sid: str, token: str) -> None:
    store = {}
    if TOKEN_STORE.exists():
        store = json.loads(TOKEN_STORE.read_text())
    store[sid] = token
    TOKEN_STORE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_STORE.write_text(json.dumps(store, indent=2))
    try:
        os.chmod(TOKEN_STORE, 0o600)  # it's a secret -- keep it private
    except OSError:
        pass


def _load_token(sid: str) -> str | None:
    if TOKEN_STORE.exists():
        return json.loads(TOKEN_STORE.read_text()).get(sid)
    return None


def _resolve_token(explicit: str | None, sid: str) -> str | None:
    """--token wins, then $GREEVILS_TOKEN, then the local store (from `submit`)."""
    return explicit or os.environ.get("GREEVILS_TOKEN") or _load_token(sid)


# ---- commands -------------------------------------------------------------

def _api_error(r: requests.Response) -> str:
    """Readable message from a non-2xx API response (FastAPI puts the reason in `detail`)."""
    try:
        detail = r.json().get("detail")
    except ValueError:
        detail = None
    return detail or r.text.strip() or f"HTTP {r.status_code} {r.reason}"


def _short_ts(iso: str | None) -> str:
    """Trim an ISO timestamp to 'YYYY-MM-DD HH:MM:SS' for table display; '-' if unset."""
    if not iso:
        return "-"
    return iso[:19].replace("T", " ")


def cmd_encrypt(args: argparse.Namespace) -> None:
    plaintext = Path(args.agent).read_bytes()
    token, key = encrypt_agent(plaintext, args.key)
    Path(args.out).write_bytes(token)
    print(f"wrote {args.out} ({len(token)} bytes ciphertext)", file=sys.stderr)
    print(f"AGENT_KEY={key}")  # secret -- save it; you pass it at deploy and it's the decrypt key


def cmd_package(args: argparse.Namespace) -> None:
    """Zip + encrypt an agent DIRECTORY (multi-file) into a submittable bundle.

    The directory MUST contain `entry.py` (the fixed entrypoint, run as `python entry.py` inside
    the TEE) and SHOULD contain `requirements.txt` (your pip deps, installed at runtime). Prints
    the AGENT_KEY (the decrypt key you pass at deploy) and the agent's SHA-256 -- the harness
    publishes the same hash at GET /agent, so you can confirm exactly which code is running.
    """
    src = Path(args.dir)
    if not src.is_dir():
        raise SystemExit(f"not a directory: {src}")
    if not (src / "entry.py").exists():
        raise SystemExit(f"{src}/entry.py not found -- it is the required entrypoint")
    if not (src / "requirements.txt").exists():
        print(f"warning: {src}/requirements.txt not found -- no dependencies will be installed",
              file=sys.stderr)
    token, key, sha256 = package_dir(str(src), args.key)
    Path(args.out).write_bytes(token)
    print(f"wrote {args.out} ({len(token)} bytes ciphertext)", file=sys.stderr)
    print(f"AGENT_KEY={key}")          # secret -- save it; needed to deploy/redeploy
    print(f"AGENT_SHA256={sha256}")    # the agent identity the harness will publish at GET /agent


def cmd_submit(args: argparse.Namespace) -> None:
    enc = Path(args.enc)
    files = {"agent": (enc.name, enc.read_bytes(), "application/octet-stream")}
    r = requests.post(f"{args.api}/submissions", files=files, data={"name": args.name}, timeout=60)
    if not r.ok:
        raise SystemExit(f"submit failed: {_api_error(r)}")
    j = r.json()
    _save_token(j["id"], j["token"])
    print(f"submitted: id={j['id']}  name={j['name']}  status={j['status']}")
    print(f"token:     {j['token']}")
    print(f"           saved to {TOKEN_STORE} (chmod 600). KEEP A COPY -- it's required to")
    print(f"           report the deploy IP, and can't be recovered from the server.")
    print(f"track it:  greevils status {j['id']} --api {args.api}")


def cmd_list(args: argparse.Namespace) -> None:
    r = requests.get(f"{args.api}/submissions", timeout=30)
    r.raise_for_status()
    rows = r.json()
    if not rows:
        print("no submissions")
        return
    print(f"{'ID':<10} {'STATUS':<16} {'HEALTH':<10} {'ATTEST':<8} {'CHECKED':<20} "
          f"{'NAME':<20} {'IP':<16} {'AGENT ACCOUNT':<44} {'MASTER ACCOUNT':<44} DIGEST")
    for s in rows:
        print(f"{s['id']:<10} {s['status']:<16} {(s.get('health') or '-'):<10} "
              f"{(s.get('attestation') or '-'):<8} {_short_ts(s.get('health_checked_at')):<20} "
              f"{s['name'][:20]:<20} {(s.get('public_ip') or '-'):<16} "
              f"{(s.get('agent_address') or '-'):<44} {(s.get('master_account') or '-'):<44} "
              f"{s.get('image_digest') or '-'}")


def cmd_status(args: argparse.Namespace) -> None:
    r = requests.get(f"{args.api}/submissions/{args.id}", timeout=30)
    if r.status_code == 404:
        raise SystemExit(f"no such submission: {args.id}")
    r.raise_for_status()
    s = r.json()
    print(f"id:            {s['id']}")
    print(f"name:          {s['name']}")
    status_display = "DEPLOYED (BOOTING)" if s['status'] == "DEPLOYED" else s['status']
    print(f"status:        {status_display}")
    print(f"health:        {s.get('health') or '-'}"
          + (f"  (checked {s['health_checked_at']})" if s.get('health_checked_at') else ""))
    att = s.get("attestation") or "-"
    att_extras = []
    if s.get("attestation_checked_at"):
        att_extras.append(f"checked {s['attestation_checked_at']}")
    if s.get("attestation_nonce"):
        att_extras.append(f"nonce {s['attestation_nonce']}")
    print(f"attestation:   {att}" + (f"  ({', '.join(att_extras)})" if att_extras else ""))
    detail = s.get("attestation_detail") or {}
    if isinstance(detail, dict) and detail.get("error"):
        print(f"               error: {detail['error']}")
    print(f"image_ref:     {s.get('image_ref') or '-'}")
    print(f"image_digest:  {s.get('image_digest') or '-'}")
    # Cached by the backend health monitor from the VM's harness (:8080); '-' until first healthy probe.
    print(f"harness_hash:  {s.get('harness_hash') or '-'}")
    print(f"agent_hash:    {s.get('agent_hash') or '-'}")
    print(f"public_ip:     {s.get('public_ip') or '-'}")
    print(f"agent_account: {s.get('agent_address') or '-'}")
    print(f"master_account:{s.get('master_account') or '-'}")
    if s.get("error"):
        print(f"error:         {s['error']}")
    if args.log and isinstance(detail, dict) and detail.get("checks"):
        print("---- attestation checks ----")
        for c in detail["checks"]:
            mark = "PASS" if c.get("ok") else "FAIL"
            print(f"  [{mark}] {c.get('check')}: actual={c.get('actual')!r} expected={c.get('expected')!r}")
    if args.log and s.get("log"):
        print("---- build log ----")
        print(s["log"])


def _report_ip(api: str, sid: str, ip: str, token: str | None) -> None:
    if not token:
        raise SystemExit(f"no submission token for {sid} -- pass --token or set GREEVILS_TOKEN "
                         f"(it was printed/saved when you ran `greevils submit`)")
    r = requests.post(f"{api}/submissions/{sid}/ip", data={"public_ip": ip},
                      headers={"X-Submission-Token": token}, timeout=30)
    if r.status_code == 404:
        raise SystemExit(f"no such submission: {sid}")
    if r.status_code == 401:
        raise SystemExit(f"submission token rejected for {sid} -- wrong token?")
    r.raise_for_status()
    print(f"reported public IP {ip} for submission {sid} (status -> DEPLOYED)")


def _parse_env_file(path: str) -> dict[str, str]:
    """Parse a .env-style file into {NAME: value}. Blank lines and `#` comments are ignored;
    each remaining line is `KEY=VALUE` (a leading `export ` is allowed, surrounding quotes on
    the value are stripped). Values are taken verbatim otherwise -- no shell expansion."""
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"no such env file: {path}")
    env: dict[str, str] = {}
    for lineno, raw in enumerate(p.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            raise SystemExit(f"{path}:{lineno}: expected KEY=VALUE, got {raw!r}")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            raise SystemExit(f"{path}:{lineno}: empty variable name")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env[key] = value
    return env


def cmd_deploy(args: argparse.Namespace) -> None:
    image_ref, image_digest = args.image_ref, args.digest
    agent_env = _parse_env_file(args.env_file) if args.env_file else None
    # Resolve image_ref/digest from a published submission unless given explicitly. PUBLISHED is
    # the first deploy; STALE means a prior deployment stopped answering, so re-deploying it (a
    # fresh VM from the same image) is allowed too.
    if args.id:
        r = requests.get(f"{args.api}/submissions/{args.id}", timeout=30)
        if r.status_code == 404:
            raise SystemExit(f"no such submission: {args.id}")
        r.raise_for_status()
        s = r.json()
        if s["status"] not in ("PUBLISHED", "STALE"):
            raise SystemExit(f"submission {args.id} is {s['status']} -- can only deploy a "
                             f"PUBLISHED submission or re-deploy a STALE one")
        image_ref = image_ref or s["image_ref"]
        image_digest = image_digest or s["image_digest"]
    if not image_ref or not image_digest:
        raise SystemExit("need <id> of a PUBLISHED submission, or both --image-ref and --digest")

    ip = deploy(
        image_ref=image_ref,
        image_digest=image_digest,
        agent_key=args.agent_key or os.environ.get("AGENT_KEY", ""),
        master_account=args.master_account or os.environ.get("MASTER_ACCOUNT", ""),
        project=args.project,
        zone=args.zone,
        vm_name=args.vm_name,
        workload_sa=args.workload_sa,
        source_range=args.source_range,
        token_backend=args.token_backend,
        ita_api_key=args.ita_api_key or os.environ.get("ITA_API_KEY"),
        ita_region=args.ita_region,
        agent_env=agent_env,
    )

    # Report the VM's public IP back to the API (only when deploying a known submission).
    # Don't let a reporting hiccup mask a successful deploy -- warn and move on.
    if args.id and ip:
        try:
            _report_ip(args.api, args.id, ip, _resolve_token(args.token, args.id))
        except (requests.RequestException, SystemExit) as e:
            print(f"WARNING: deployed OK, but couldn't report the IP to the API: {e}\n"
                  f"         report it manually:\n"
                  f"         curl -X POST {args.api}/submissions/{args.id}/ip "
                  f"-F public_ip={ip} -H 'X-Submission-Token: <your token>'",
                  file=sys.stderr)


def cmd_commit(args: argparse.Namespace) -> None:
    """Publish an on-chain Hyperliquid ownership commitment for this hotkey.

    This is the miner's entire job: register a neuron, then run this once to claim the
    Hyperliquid account (agent or normal trading account) the validator should score you on.
    """
    from . import commit as commitlib

    try:
        import bittensor as bt
    except ImportError:
        raise SystemExit("bittensor is required for `commit` -- run `pip install -e .` in greevils-cli")

    wallet = bt.Wallet(name=args.wallet_name, hotkey=args.hotkey)
    hotkey_ss58 = wallet.hotkey.ss58_address

    # The miner produces the signature in the web UI (we never handle the Hyperliquid account
    # key). The signed message is the canonical message, rebuilt here from (hotkey, address) --
    # it is not stored on-chain, so the validator rebuilds it the same way.
    if not (args.hl_address and args.signature):
        raise SystemExit(
            "provide both --hl-address and --signature (the signature you produced in the web UI)"
        )
    hl_address = args.hl_address
    signature = commitlib._normalize_sig_hex(args.signature)

    # Self-verify exactly as the validator will, before paying for the extrinsic.
    ok, reason = commitlib.verify_commitment(hotkey_ss58, hl_address, signature)
    if not ok:
        raise SystemExit(f"refusing to commit -- {reason}")

    data = commitlib.encode_commitment(hl_address, signature)
    print(f"hotkey:        {hotkey_ss58}")
    print(f"hl_address:    {hl_address}")
    print(f"commitment:    {data}  ({len(data.encode())} bytes)")
    if args.dry_run:
        print("dry-run: nothing written on-chain")
        return

    subtensor = bt.Subtensor(network=args.network)
    resp = subtensor.set_commitment(wallet=wallet, netuid=args.netuid, data=data)
    success = getattr(resp, "is_success", None)
    if success is None:
        success = bool(resp)
    if success:
        print(f"committed on netuid {args.netuid} (network={args.network}). The validator will pick "
              f"it up on its next round.")
    else:
        raise SystemExit(f"set_commitment did not succeed: {resp}")


def cmd_verify(args: argparse.Namespace) -> None:
    """Verify a Hyperliquid ownership commitment locally -- exactly the check the validator runs.

    Pure and offline: rebuilds the canonical message from (hotkey, hl_address) and checks the
    signature recovers to hl_address. Nothing is encoded or written on-chain. The hotkey can be
    given directly with --hotkey-ss58, or derived from a local Bittensor wallet.
    """
    from . import commit as commitlib

    if not (args.hl_address and args.signature):
        raise SystemExit("provide both --hl-address and --signature")

    hotkey_ss58 = args.hotkey_ss58
    if not hotkey_ss58:
        try:
            import bittensor as bt
        except ImportError:
            raise SystemExit("pass --hotkey-ss58, or install bittensor to derive it from a wallet "
                             "(`pip install -e .` in greevils-cli)")
        wallet = bt.Wallet(name=args.wallet_name, hotkey=args.hotkey)
        hotkey_ss58 = wallet.hotkey.ss58_address

    signature = commitlib._normalize_sig_hex(args.signature)
    ok, reason = commitlib.verify_commitment(hotkey_ss58, args.hl_address, signature)
    print(f"hotkey:     {hotkey_ss58}")
    print(f"hl_address: {args.hl_address}")
    if not ok:
        raise SystemExit(f"verify:     FAIL -- {reason}")
    print("verify:     OK -- signature recovers to the claimed hl_address")


def _read_digests_file(path: str) -> list[str]:
    """Read approved digests from `path` -- its content must be a JSON array of digest strings."""
    try:
        digests = json.loads(Path(path).read_text())
    except FileNotFoundError:
        raise SystemExit(f"no such file: {path}")
    except json.JSONDecodeError as e:
        raise SystemExit(f"{path}: not valid JSON ({e})")
    if not isinstance(digests, list) or not all(isinstance(d, str) for d in digests):
        raise SystemExit(f"{path}: expected a JSON array of image-digest strings")
    return digests


def cmd_approve(args: argparse.Namespace) -> None:
    """Publish the set of approved agent image digests for your hotkey.

    Two steps: (1) POST the list + an sr25519 signature to greevils-api (proving you own the
    hotkey), then (2) commit the list's hash on-chain so validators can verify the published
    list. The list is the FULL approved set -- it replaces whatever your hotkey had before.
    """
    from . import approve as approvelib

    try:
        import bittensor as bt
    except ImportError:
        raise SystemExit("bittensor is required for `approve` -- run `pip install -e .` in greevils-cli")

    digests = _read_digests_file(args.file)
    if not digests:
        raise SystemExit(f"{args.file} is an empty list -- nothing to approve")

    wallet = bt.Wallet(name=args.wallet_name, hotkey=args.hotkey)
    hotkey_ss58 = wallet.hotkey.ss58_address

    canonical = approvelib.canonical_digests(digests)
    message = approvelib.approval_message(hotkey_ss58, approvelib.list_hash_b64(canonical))
    signature = "0x" + wallet.hotkey.sign(message.encode()).hex()

    # 1. Publish the list to greevils-api (it verifies the signature before storing).
    resp = requests.post(f"{args.api}/approved/{hotkey_ss58}",
                         json={"digests": canonical, "signature": signature}, timeout=30)
    if not resp.ok:
        raise SystemExit(f"greevils-api rejected the list ({resp.status_code}): {resp.text}")
    print(f"published {len(canonical)} approved digest(s) for {hotkey_ss58}")

    # 2. Commit the list's hash on-chain so validators can verify the published list.
    data = approvelib.encode_commitment(canonical)
    print(f"commitment:    {data}  ({len(data.encode())} bytes)")
    if args.dry_run:
        print("dry-run: nothing written on-chain")
        return

    subtensor = bt.Subtensor(network=args.network)
    r = subtensor.set_commitment(wallet=wallet, netuid=args.netuid, data=data)
    success = getattr(r, "is_success", None)
    if success is None:
        success = bool(r)
    if success:
        print(f"committed approval hash on netuid {args.netuid} (network={args.network}). The "
              f"validator honours it only if your hotkey is the highest-staked validator.")
    else:
        raise SystemExit(f"set_commitment did not succeed: {r}")


# ---- arg parsing ----------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="greevils", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_api(p: argparse.ArgumentParser) -> None:
        p.add_argument("--api", default=DEFAULT_API, help=f"greevils-api base URL (default {DEFAULT_API})")

    p = sub.add_parser("package",
                       help="zip + encrypt a multi-file agent DIRECTORY -> bundle + AGENT_KEY")
    p.add_argument("dir", help="path to your agent directory (must contain entry.py)")
    p.add_argument("-o", "--out", default="agent-bundle.enc", help="output bundle ciphertext path")
    p.add_argument("--key", help="reuse an existing AGENT_KEY (else a fresh one is generated)")
    p.set_defaults(func=cmd_package)

    p = sub.add_parser("encrypt", help="(legacy) encrypt a single agent.py -> ciphertext + AGENT_KEY")
    p.add_argument("agent", help="path to your plaintext agent.py")
    p.add_argument("-o", "--out", default="agent.py.enc", help="output ciphertext path")
    p.add_argument("--key", help="reuse an existing AGENT_KEY (else a fresh one is generated)")
    p.set_defaults(func=cmd_encrypt)

    p = sub.add_parser("submit", help="upload an encrypted agent bundle to greevils-api")
    p.add_argument("enc", help="path to agent-bundle.enc (from `greevils package`)")
    p.add_argument("--name", required=True, help="a name for your agent submission")
    add_api(p)
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("list", help="list all submissions")
    add_api(p)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("status", help="show one submission's status + image digest")
    p.add_argument("id", help="submission id")
    p.add_argument("--log", action="store_true",
                   help="also print the per-claim attestation checks and the build log")
    add_api(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("deploy", help="deploy a PUBLISHED submission into a CS TDX VM")
    p.add_argument("id", nargs="?", help="submission id (resolves image_ref + digest from the API)")
    add_api(p)  # only used when <id> is given (to look up the published image_ref + digest)
    p.add_argument("--image-ref", help="override: image ref (instead of resolving from <id>)")
    p.add_argument("--digest", help="override: image digest (instead of resolving from <id>)")
    p.add_argument("--agent-key", help="AGENT_KEY (default: $AGENT_KEY)")
    p.add_argument("--master-account", help="0x... where funds return (default: $MASTER_ACCOUNT)")
    p.add_argument("--env-file", metavar="PATH",
                   help="a .env-style file (KEY=VALUE per line) of env vars to pass to your "
                        "agent inside the TEE, e.g. API keys. Stays in your own VM metadata.")
    p.add_argument("--token", help="submission token for IP report (default: $GREEVILS_TOKEN or local store)")
    p.add_argument("--project", default=os.environ.get("GREEVILS_GCP_PROJECT", "calcium-arcadia-464813-j4"),
                   help="your GCP project (where the VM runs)")
    p.add_argument("--zone", default=os.environ.get("GREEVILS_GCP_ZONE", "us-central1-a"),
                   help="zone (must support c3 + TDX)")
    p.add_argument("--vm-name", default=os.environ.get("GREEVILS_VM_NAME", "greevils-cs-vm"))
    p.add_argument("--workload-sa", help="workload service account (default greevils-workload@PROJECT...)")
    p.add_argument("--source-range", default="0.0.0.0/0", help="firewall source range for :8443/:8080")
    p.add_argument("--token-backend", default=os.environ.get("CS_TOKEN_BACKEND", "google"),
                   choices=["google", "ita"], help="attestation backend")
    p.add_argument("--ita-api-key", help="Intel Trust Authority key (only for --token-backend ita)")
    p.add_argument("--ita-region", default="US")
    p.set_defaults(func=cmd_deploy)

    p = sub.add_parser("commit", help="claim a Hyperliquid account on-chain (the miner's job)")
    p.add_argument("--network", default=os.environ.get("NETWORK", "finney"),
                   help="subtensor network: finney, test, local (default finney)")
    p.add_argument("--netuid", type=int, default=int(os.environ.get("NETUID", "1")),
                   help="subnet netuid (default 1)")
    p.add_argument("--wallet-name", "--coldkey", default=os.environ.get("WALLET_NAME", "default"),
                   help="coldkey / wallet name (default 'default')")
    p.add_argument("--hotkey", default=os.environ.get("HOTKEY_NAME", "default"),
                   help="hotkey name -- must be the neuron registered on the subnet (default 'default')")
    p.add_argument("--hl-address", help="claimed Hyperliquid account address")
    p.add_argument("--signature", help="EIP-191 personal_sign signature over the canonical message (hex)")
    p.add_argument("--dry-run", action="store_true",
                   help="build + self-verify the commitment and print it, without writing on-chain")
    p.set_defaults(func=cmd_commit)

    p = sub.add_parser("verify",
                       help="locally verify a Hyperliquid ownership commitment (no on-chain write)")
    p.add_argument("--hl-address", help="claimed Hyperliquid account address")
    p.add_argument("--signature", help="EIP-191 personal_sign signature over the canonical message (hex)")
    p.add_argument("--hotkey-ss58",
                   help="hotkey ss58 to bind the message to (skips the wallet lookup -- no bittensor needed)")
    p.add_argument("--wallet-name", "--coldkey", default=os.environ.get("WALLET_NAME", "default"),
                   help="coldkey / wallet name, used only if --hotkey-ss58 is omitted (default 'default')")
    p.add_argument("--hotkey", default=os.environ.get("HOTKEY_NAME", "default"),
                   help="hotkey name, used only if --hotkey-ss58 is omitted (default 'default')")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("approve",
                       help="publish your hotkey's approved agent image digests (validator approval)")
    p.add_argument("file",
                   help="path to a JSON file: an array of image digests -- the FULL approved set "
                        "(replaces your previous list)")
    add_api(p)
    p.add_argument("--network", default=os.environ.get("NETWORK", "finney"),
                   help="subtensor network: finney, test, local (default finney)")
    p.add_argument("--netuid", type=int, default=int(os.environ.get("NETUID", "1")),
                   help="subnet netuid (default 1)")
    p.add_argument("--wallet-name", "--coldkey", default=os.environ.get("WALLET_NAME", "default"),
                   help="coldkey / wallet name (default 'default')")
    p.add_argument("--hotkey", default=os.environ.get("HOTKEY_NAME", "default"),
                   help="hotkey name whose approved list to publish (default 'default')")
    p.add_argument("--dry-run", action="store_true",
                   help="publish to the API + print the commitment, without writing on-chain")
    p.set_defaults(func=cmd_approve)

    return ap


def main() -> None:
    args = build_parser().parse_args()
    try:
        args.func(args)
    except requests.RequestException as e:
        raise SystemExit(f"API request failed: {e}")


if __name__ == "__main__":
    main()
