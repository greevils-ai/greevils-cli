#!/usr/bin/env python3
"""greevils -- the participant CLI. Everything the participant does runs through here.

  greevils encrypt agent.py [-o agent.py.enc]      # generate AGENT_KEY + write ciphertext
  greevils submit agent.py.enc --name my-agent     # upload ciphertext to greevils-api
  greevils list                                    # all submissions (spot your own by name)
  greevils status <id>                             # one submission's status + image digest
  greevils deploy <id> --agent-key K --master-account 0x...   # launch the CS TDX VM

encrypt + deploy are fully local (the API never sees plaintext or your key). submit/list/
status just talk to the greevils-api backend (--api or GREEVILS_API, default https://api.greevils.ai).
"""
import argparse
import json
import os
import sys
from pathlib import Path

import requests

from .crypto import encrypt_agent
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


def cmd_submit(args: argparse.Namespace) -> None:
    enc = Path(args.enc)
    files = {"agent": (enc.name, enc.read_bytes(), "application/octet-stream")}
    r = requests.post(f"{args.api}/submissions", files=files, data={"name": args.name}, timeout=60)
    r.raise_for_status()
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
    print(f"status:        {s['status']}")
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


def cmd_deploy(args: argparse.Namespace) -> None:
    image_ref, image_digest = args.image_ref, args.digest
    # Resolve image_ref/digest from a published submission unless given explicitly.
    if args.id:
        r = requests.get(f"{args.api}/submissions/{args.id}", timeout=30)
        if r.status_code == 404:
            raise SystemExit(f"no such submission: {args.id}")
        r.raise_for_status()
        s = r.json()
        if s["status"] != "PUBLISHED":
            raise SystemExit(f"submission {args.id} is {s['status']}, not PUBLISHED -- can't deploy yet")
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


# ---- arg parsing ----------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="greevils", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_api(p: argparse.ArgumentParser) -> None:
        p.add_argument("--api", default=DEFAULT_API, help=f"greevils-api base URL (default {DEFAULT_API})")

    p = sub.add_parser("encrypt", help="encrypt agent.py -> ciphertext + AGENT_KEY")
    p.add_argument("agent", help="path to your plaintext agent.py")
    p.add_argument("-o", "--out", default="agent.py.enc", help="output ciphertext path")
    p.add_argument("--key", help="reuse an existing AGENT_KEY (else a fresh one is generated)")
    p.set_defaults(func=cmd_encrypt)

    p = sub.add_parser("submit", help="upload an encrypted agent to greevils-api")
    p.add_argument("enc", help="path to agent.py.enc (from `greevils encrypt`)")
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

    return ap


def main() -> None:
    args = build_parser().parse_args()
    try:
        args.func(args)
    except requests.RequestException as e:
        raise SystemExit(f"API request failed: {e}")


if __name__ == "__main__":
    main()
