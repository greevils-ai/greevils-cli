"""On-chain Hyperliquid ownership commitment -- the miner side.

A miner claims a Hyperliquid account (an agent/API wallet or a normal trading account)
by publishing a tiny JSON commitment to the subnet via Bittensor's `set_commitment`:

    {"v": 1, "hl_address": "0x..", "message": "..", "signature": "0x.."}

`signature` is an EIP-191 `personal_sign` over `message`, produced by the Hyperliquid
account's private key. The validator later recovers the signer from (message, signature)
and checks it equals `hl_address` -- which proves the miner controls that account.

The canonical message embeds the miner's **hotkey ss58**, so the commitment is bound to
the miner's identity: another miner can't copy your on-chain commitment and claim the same
Hyperliquid account as theirs.

The miner produces `signature` in the web UI (the CLI never handles the Hyperliquid key).

This module is pure (no Bittensor): it builds and self-verifies the commitment payload.
The actual on-chain write (`set_commitment`) is done by the CLI in cli.py. `eth_account` is
imported lazily so importing the CLI never requires it unless you commit.
"""
import json

COMMITMENT_VERSION = 1


def canonical_message(hotkey_ss58: str, hl_address: str) -> str:
    """The default message a miner signs to prove ownership of `hl_address`.

    Embeds the hotkey so the signed claim can't be stolen by another miner. The validator
    only requires that the *hotkey* appears in the signed message (and that the signature
    recovers `hl_address`), so a custom message is fine too as long as it contains the
    hotkey ss58.
    """
    return (
        "Greevils Hyperliquid ownership claim\n"
        f"hotkey: {hotkey_ss58}\n"
        f"hyperliquid: {hl_address}"
    )


def _normalize_sig_hex(signature: str) -> str:
    """Accept a 0x-prefixed or bare hex signature; return it 0x-prefixed and lowercased."""
    s = signature.strip().lower()
    if not s.startswith("0x"):
        s = "0x" + s
    int(s, 16)  # raises ValueError if it isn't hex -- fail early, before paying for the extrinsic
    return s


def verify_commitment(hotkey_ss58: str, hl_address: str, message: str,
                      signature: str) -> tuple[bool, str]:
    """Locally re-check a commitment exactly as the validator will.

    Returns (ok, reason). The CLI runs this before writing on-chain so a bad signature is
    caught for free instead of after paying for the extrinsic.
    """
    from eth_account import Account
    from eth_account.messages import encode_defunct

    if hotkey_ss58 not in message:
        return False, "signed message does not reference this hotkey (ownership-binding check)"
    try:
        recovered = Account.recover_message(encode_defunct(text=message), signature=signature)
    except Exception as e:  # noqa: BLE001 -- surface any decode/recover failure verbatim
        return False, f"signature recovery failed: {e}"
    if recovered.lower() != hl_address.lower():
        return False, f"signature recovers {recovered}, not the claimed {hl_address}"
    return True, ""


def encode_commitment(hl_address: str, message: str, signature: str) -> str:
    """Serialize the commitment to the compact JSON string stored on-chain."""
    return json.dumps(
        {
            "v": COMMITMENT_VERSION,
            "hl_address": hl_address,
            "message": message,
            "signature": signature,
        },
        separators=(",", ":"),
    )
