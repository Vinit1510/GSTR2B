"""Master-password protected credential vault.

Design:
- User picks a master password on first run.
- We derive a 32-byte key from it via PBKDF2-HMAC-SHA256 (390 000 iters,
  random 16-byte salt) and use that key with Fernet (AES-128-CBC + HMAC).
- A small "verification token" is written to the vault so we can detect
  wrong master passwords without leaking anything about the contents.
- The master password is NEVER stored on disk; the user must enter it
  every launch.
"""
from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from . import config

_PBKDF2_ITERATIONS = 390_000
_VERIFICATION_PLAINTEXT = b"gstr2b-vault-ok-v1"


@dataclass
class Vault:
    """In-memory unlocked vault."""

    fernet: Fernet
    salt: bytes

    def encrypt(self, value: str) -> str:
        return self.fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        return self.fernet.decrypt(token.encode("ascii")).decode("utf-8")


def _derive_key(master_password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=_PBKDF2_ITERATIONS,
    )
    raw = kdf.derive(master_password.encode("utf-8"))
    return base64.urlsafe_b64encode(raw)


def vault_exists() -> bool:
    return config.VAULT_FILE.exists()


def create_vault(master_password: str) -> Vault:
    """First-time setup. Writes a salt + verification token to disk."""
    config.ensure_dirs()
    if vault_exists():
        raise RuntimeError("Vault already initialised; use unlock_vault().")

    salt = os.urandom(16)
    key = _derive_key(master_password, salt)
    fernet = Fernet(key)
    verification = fernet.encrypt(_VERIFICATION_PLAINTEXT).decode("ascii")

    payload = {
        "salt": base64.b64encode(salt).decode("ascii"),
        "verification": verification,
        "version": 1,
    }
    config.VAULT_FILE.write_text(json.dumps(payload), encoding="utf-8")
    return Vault(fernet=fernet, salt=salt)


def unlock_vault(master_password: str) -> Vault:
    """Open existing vault. Raises ValueError on wrong master password."""
    if not vault_exists():
        raise FileNotFoundError("Vault not initialised. Call create_vault first.")

    payload = json.loads(config.VAULT_FILE.read_text(encoding="utf-8"))
    salt = base64.b64decode(payload["salt"])
    key = _derive_key(master_password, salt)
    fernet = Fernet(key)

    try:
        decrypted = fernet.decrypt(payload["verification"].encode("ascii"))
    except InvalidToken as exc:
        raise ValueError("Wrong master password.") from exc

    if decrypted != _VERIFICATION_PLAINTEXT:
        raise ValueError("Vault verification mismatch.")

    return Vault(fernet=fernet, salt=salt)


def reset_vault(confirm: bool = False) -> None:
    """Destroy the vault file. Requires explicit confirm=True."""
    if not confirm:
        raise ValueError("Pass confirm=True to actually delete the vault.")
    if config.VAULT_FILE.exists():
        config.VAULT_FILE.unlink()
