"""On-chain Hyperliquid ownership commitment -- the miner side.

A miner claims a Hyperliquid account (an agent/API wallet or a normal trading account)
by publishing a compact commitment to the subnet via Bittensor's `set_commitment`. The
on-chain Raw commitment field is capped at 128 bytes, so we store ONLY what the validator
can't reconstruct:

    base64( hl_address(20 bytes) || signature(65 bytes) )   -- 116 chars

`signature` is an EIP-191 `personal_sign` over the *canonical message* (see
`canonical_message`), produced by the Hyperliquid account's private key. The signed message
is NOT stored on-chain: the validator rebuilds it deterministically from the committing
hotkey + the embedded `hl_address`, then recovers the signer and checks it equals
`hl_address` -- which proves the miner controls that account.

Because the message is rebuilt from the *committing* hotkey, the claim is bound to the
miner's identity for free: another miner copying your blob would have the message rebuilt
with THEIR hotkey, so the signature would no longer recover to `hl_address` and the copy is
rejected. (Hex encoding would not fit -- the 65-byte signature alone is 132 hex chars > 128.)

The miner produces `signature` in the web UI (the CLI never handles the Hyperliquid key).

This module is pure (no Bittensor): it builds and self-verifies the commitment payload.
The actual on-chain write (`set_commitment`) is done by the CLI in cli.py. `eth_account` is
imported lazily so importing the CLI never requires it unless you commit.
"""
import base64

# Fixed byte widths of the two fields packed into the on-chain blob.
_ADDR_BYTES = 20   # an Ethereum/Hyperliquid address
_SIG_BYTES = 65    # an EIP-191 signature (r=32, s=32, v=1)


def canonical_message(hotkey_ss58: str, hl_address: str) -> str:
    """The message a miner signs to prove ownership of `hl_address`.

    Embeds the hotkey so the signed claim can't be stolen by another miner, and the address
    so the signature is bound to the specific account. The validator rebuilds this exact
    string (it is not stored on-chain), so the construction must stay byte-for-byte in
    agreement with greevils-validator and the TEE harness.

    The address is lowercased so signer and validator agree regardless of EIP-55 checksum
    casing -- the validator rebuilds it from raw bytes (lowercase), so the signer must too.
    """
    return (
        "Greevils Hyperliquid ownership claim\n"
        f"hotkey: {hotkey_ss58}\n"
        f"hyperliquid: {hl_address.lower()}"
    )


def _normalize_sig_hex(signature: str) -> str:
    """Accept a 0x-prefixed or bare hex signature; return it 0x-prefixed and lowercased."""
    s = signature.strip().lower()
    if not s.startswith("0x"):
        s = "0x" + s
    int(s, 16)  # raises ValueError if it isn't hex -- fail early, before paying for the extrinsic
    return s


def verify_commitment(hotkey_ss58: str, hl_address: str, signature: str) -> tuple[bool, str]:
    """Locally re-check a commitment exactly as the validator will.

    Rebuilds the canonical message from (hotkey, hl_address) and checks the signature
    recovers to `hl_address`. Returns (ok, reason). The CLI runs this before writing on-chain
    so a bad signature is caught for free instead of after paying for the extrinsic.
    """
    from eth_account import Account
    from eth_account.messages import encode_defunct

    message = canonical_message(hotkey_ss58, hl_address)
    try:
        recovered = Account.recover_message(encode_defunct(text=message), signature=signature)
    except Exception as e:  # noqa: BLE001 -- surface any decode/recover failure verbatim
        return False, f"signature recovery failed: {e}"
    if recovered.lower() != hl_address.lower():
        return False, f"signature recovers {recovered}, not the claimed {hl_address}"
    return True, ""


def encode_commitment(hl_address: str, signature: str) -> str:
    """Pack (hl_address, signature) into the compact base64 blob stored on-chain.

    Layout: base64( address(20B) || signature(65B) ) -- 116 chars, under the 128-byte Raw
    commitment limit. The signed message is intentionally omitted; the validator rebuilds it.
    """
    addr_hex = hl_address[2:] if hl_address.lower().startswith("0x") else hl_address
    addr = bytes.fromhex(addr_hex)
    sig = bytes.fromhex(_normalize_sig_hex(signature)[2:])
    if len(addr) != _ADDR_BYTES:
        raise ValueError(f"hl_address must be {_ADDR_BYTES} bytes, got {len(addr)}")
    if len(sig) != _SIG_BYTES:
        raise ValueError(f"signature must be {_SIG_BYTES} bytes, got {len(sig)}")
    return base64.b64encode(addr + sig).decode()
