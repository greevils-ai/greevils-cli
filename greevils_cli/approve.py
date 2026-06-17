"""Build the payload for `greevils approve` -- a hotkey's approved agent image-digest list.

Approving agents is a two-step publish (see cli.py:cmd_approve):
  1. POST the canonical digest list + an sr25519 signature to greevils-api, which stores it
     under your hotkey (the signature proves you own the hotkey).
  2. Commit `gva1:<base64(sha256(canonical_list))>` on-chain, so a validator can verify the
     greevils-api list hasn't been tampered with.

This module is pure (no Bittensor): it canonicalizes, hashes and builds the signed message and
the on-chain commitment string. The canonical serialization + hash MUST stay byte-for-byte
identical to greevils-api (app/approvals.py) and greevils-validator
(greevils_validator/approvals.py), or the on-chain hash won't match the stored list.
"""
import base64
import hashlib
import json

# Namespace tag marking a commitment as a greevils approval hash (v1).
APPROVAL_TAG = "gva1:"


def normalize_digest(digest: str) -> str:
    """Canonicalize one image digest: drop any `algo:` prefix, lowercase, trim."""
    return digest.strip().lower().rsplit(":", 1)[-1]


def canonical_digests(digests: list[str]) -> list[str]:
    """Normalize, drop blanks, dedupe and sort -- the order-independent canonical form."""
    return sorted({normalize_digest(d) for d in digests if isinstance(d, str) and d.strip()})


def list_hash_b64(digests: list[str]) -> str:
    """base64(sha256(json.dumps(canonical_digests))) -- the value committed on-chain."""
    blob = json.dumps(canonical_digests(digests), separators=(",", ":")).encode()
    return base64.b64encode(hashlib.sha256(blob).digest()).decode()


def approval_message(hotkey: str, hash_b64: str) -> str:
    """The message the hotkey owner signs to authorize publishing this list for this hotkey."""
    return f"greevils approved-image-digests\nhotkey: {hotkey}\nsha256: {hash_b64}"


def encode_commitment(digests: list[str]) -> str:
    """The on-chain commitment string: the tagged base64 sha256 of the canonical list."""
    return APPROVAL_TAG + list_hash_b64(digests)
