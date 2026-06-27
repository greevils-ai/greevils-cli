"""Agent packaging + encryption -- the participant-side crypto.

The participant's agent is now a multi-file BUNDLE: a directory (with a required `entry.py`
entrypoint and an optional `requirements.txt`) zipped and encrypted with a symmetric Fernet key.
The SAME key
decrypts it inside the TEE (passed at deploy as tee-env-AGENT_KEY). Keep the key secret; without
it the bundle can't be decrypted and you can't redeploy.

The harness publishes `sha256(bundle.zip)` at GET /agent as the running agent's identity; this
module zips deterministically (sorted entries, fixed timestamps) and reports that same hash, so
the participant can verify exactly which code is running.
"""
import hashlib
import io
import os
import zipfile

from cryptography.fernet import Fernet


# Build artifacts / VCS / venv junk excluded from the bundle, so a participant's local cruft
# doesn't bloat the upload or perturb the agent hash. Pruned by directory name or file suffix.
_EXCLUDE_DIRS = {"__pycache__", ".git", ".hg", ".svn", ".venv", "venv", ".mypy_cache",
                 ".pytest_cache", ".ruff_cache", "node_modules", ".idea", ".vscode"}
_EXCLUDE_SUFFIXES = (".pyc", ".pyo")
_EXCLUDE_FILES = {".DS_Store"}


def zip_dir(src_dir: str) -> bytes:
    """Deterministically zip a directory tree into bytes (sorted entries, fixed timestamps,
    build/VCS junk excluded), so the same source always produces the same archive -- and
    therefore the same agent hash the harness publishes."""
    src = os.path.abspath(src_dir)
    if not os.path.isdir(src):
        raise NotADirectoryError(src)
    entries: list[tuple[str, str]] = []
    for root, dirs, files in os.walk(src):
        dirs[:] = sorted(d for d in dirs if d not in _EXCLUDE_DIRS)
        for fn in files:
            if fn in _EXCLUDE_FILES or fn.endswith(_EXCLUDE_SUFFIXES):
                continue
            full = os.path.join(root, fn)
            entries.append((full, os.path.relpath(full, src)))
    entries.sort(key=lambda p: p[1])

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for full, arc in entries:
            info = zipfile.ZipInfo(arc, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with open(full, "rb") as f:
                zf.writestr(info, f.read())
    return buf.getvalue()


def encrypt_bundle(bundle: bytes, key: str | None = None) -> tuple[bytes, str, str]:
    """Encrypt a bundle. Returns (ciphertext, agent_key, sha256_hex_of_plaintext_bundle).

    The sha256 is the agent identity the harness will publish at GET /agent.
    """
    k = key.encode() if key else Fernet.generate_key()
    token = Fernet(k).encrypt(bundle)
    return token, k.decode(), hashlib.sha256(bundle).hexdigest()


def package_dir(src_dir: str, key: str | None = None) -> tuple[bytes, str, str]:
    """Zip + encrypt an agent directory. Returns (ciphertext, agent_key, agent_sha256)."""
    return encrypt_bundle(zip_dir(src_dir), key)


def encrypt_agent(plaintext: bytes, key: str | None = None) -> tuple[bytes, str]:
    """Legacy single-blob encryption (kept for compatibility). Prefer `package_dir`."""
    k = key.encode() if key else Fernet.generate_key()
    token = Fernet(k).encrypt(plaintext)
    return token, k.decode()
