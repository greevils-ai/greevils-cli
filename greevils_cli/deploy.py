"""Participant-side deploy -- launch the organizer's image (by digest) into a GCP
Confidential Space TDX VM.

The image is content-addressed, so deploying `image_ref@image_digest` cannot alter what
runs: swap the image and the attested digest changes. AGENT_KEY (decrypts the agent inside
the TEE) and MASTER_ACCOUNT (where funds are swept on wind-down, and the account that
authorizes the privileged API) travel only in the participant's own VM metadata as
tee-env-* values the image allow-lists.
"""
import base64
import json
import subprocess
import sys
import time


def _ok(cmd: list[str]) -> bool:
    return subprocess.run(cmd, capture_output=True).returncode == 0


def _run(cmd: list[str], *, capture: bool = False) -> subprocess.CompletedProcess:
    print("    $ " + " ".join(cmd))
    p = subprocess.run(cmd, text=True, capture_output=capture)
    if p.returncode != 0:
        if capture and p.stderr:
            print(p.stderr, file=sys.stderr)
        raise SystemExit(f"command failed: {' '.join(cmd)}")
    return p


def deploy(
    *,
    image_ref: str,
    image_digest: str,
    agent_key: str,
    master_account: str,
    project: str,
    zone: str,
    vm_name: str,
    workload_sa: str | None = None,
    source_range: str = "0.0.0.0/0",
    token_backend: str = "google",
    ita_api_key: str | None = None,
    ita_region: str = "US",
    agent_env: dict[str, str] | None = None,
) -> str:
    if not agent_key:
        raise SystemExit("AGENT_KEY required (--agent-key or AGENT_KEY env)")
    if not master_account:
        raise SystemExit("MASTER_ACCOUNT required (--master-account or MASTER_ACCOUNT env)")
    if token_backend == "ita" and not ita_api_key:
        raise SystemExit("ITA_API_KEY required for --token-backend ita (or use google)")

    workload_sa = workload_sa or f"greevils-workload@{project}.iam.gserviceaccount.com"
    sa_id = workload_sa.split("@")[0]
    ref = f"{image_ref}@{image_digest}"
    print(f"==> deploying {ref}")

    print("==> 0. preflight (auth, APIs)")
    if not _ok(["gcloud", "auth", "print-access-token"]):
        raise SystemExit("no usable gcloud credentials -- run 'gcloud auth login' first")
    _run(["gcloud", "services", "enable", "compute.googleapis.com",
          "confidentialcomputing.googleapis.com", "--project", project, "--quiet"])

    print("==> 1. workload service account (idempotent) + minimal roles")
    if not _ok(["gcloud", "iam", "service-accounts", "describe", workload_sa, "--project", project]):
        _run(["gcloud", "iam", "service-accounts", "create", sa_id, "--project", project,
              "--display-name", "Greevils Confidential Space workload"])
        for _ in range(12):
            if _ok(["gcloud", "iam", "service-accounts", "describe", workload_sa, "--project", project]):
                break
            time.sleep(5)
    for role in ("roles/artifactregistry.reader", "roles/logging.logWriter",
                 "roles/confidentialcomputing.workloadUser"):
        _run(["gcloud", "projects", "add-iam-policy-binding", project,
              "--member", f"serviceAccount:{workload_sa}", "--role", role,
              "--condition=None", "--quiet"], capture=True)

    print(f"==> 2. launch the Confidential Space VM (production, TDX) running image@digest (backend={token_backend})")
    metadata = (
        f"tee-image-reference={ref}"
        "~tee-container-log-redirect=true"
        f"~tee-env-CS_TOKEN_BACKEND={token_backend}"
        f"~tee-env-AGENT_KEY={agent_key}"
        f"~tee-env-MASTER_ACCOUNT={master_account}"
    )
    # The participant's own env vars (API keys etc.) ride along as ONE base64(JSON) blob the
    # TEE harness unpacks into os.environ. Base64 so a value can't contain the `^~^` metadata
    # separator; one blob so we don't have to allow-list each name in the image. These stay in
    # the participant's own VM metadata -- the organizer never sees them.
    if agent_env:
        blob = base64.b64encode(json.dumps(agent_env).encode()).decode()
        metadata += f"~tee-env-AGENT_ENV={blob}"
    if token_backend == "ita":
        metadata += f"~ita-api-key={ita_api_key}~ita-region={ita_region}"
    _run(["gcloud", "compute", "instances", "create", vm_name,
          "--project", project, "--zone", zone,
          "--machine-type=c3-standard-4",
          "--confidential-compute-type=TDX",
          "--maintenance-policy=TERMINATE",
          "--shielded-secure-boot",
          "--image-family=confidential-space",
          "--image-project=confidential-space-images",
          "--service-account", workload_sa,
          "--scopes=cloud-platform",
          "--metadata=^~^" + metadata])

    print("==> 3. firewall: allow attestation :8443 + participant API/UI :8080 (idempotent)")
    if _ok(["gcloud", "compute", "firewall-rules", "describe", "allow-attest-8443", "--project", project]):
        _run(["gcloud", "compute", "firewall-rules", "update", "allow-attest-8443",
              "--project", project, "--rules=tcp:8443,tcp:8080",
              "--source-ranges", source_range, "--quiet"])
    else:
        _run(["gcloud", "compute", "firewall-rules", "create", "allow-attest-8443",
              "--project", project, "--network=default", "--direction=INGRESS",
              "--action=ALLOW", "--rules=tcp:8443,tcp:8080",
              "--source-ranges", source_range, "--quiet"])

    print("==> done. external IP (give this + the digest to the verifier):")
    ip = _run(["gcloud", "compute", "instances", "describe", vm_name,
               "--project", project, "--zone", zone,
               "--format=value(networkInterfaces[0].accessConfigs[0].natIP)"],
              capture=True).stdout.strip()
    print(f"    {ip}")
    return ip
