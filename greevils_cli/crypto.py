"""Agent encryption -- the participant-side crypto.

Symmetric Fernet: the SAME key encrypts before submission and decrypts inside the TEE
(passed at deploy as tee-env-AGENT_KEY). Keep the key secret; without it the agent can't
be decrypted and you can't redeploy.
"""
from cryptography.fernet import Fernet


def encrypt_agent(plaintext: bytes, key: str | None = None) -> tuple[bytes, str]:
    """Encrypt agent source. Returns (ciphertext, agent_key). Generates a key if none given."""
    k = key.encode() if key else Fernet.generate_key()
    token = Fernet(k).encrypt(plaintext)
    return token, k.decode()
