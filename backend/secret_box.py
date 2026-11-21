"""Chiffrement des secrets stockés en base (clés fournisseurs des utilisateurs).

Fernet (AES-128-CBC + HMAC), clé fournie par TOKEN_ENC_KEY (la même que pour les jetons Google).
Échoue FERMÉ : sans clé valide, rien n'est écrit en clair — l'appelant reçoit SecretBoxUnavailable.
Génération : python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

PREFIX = "enc:v1:"


class SecretBoxUnavailable(RuntimeError):
    """TOKEN_ENC_KEY absent ou invalide."""


def _fernet() -> Fernet:
    key = os.getenv("TOKEN_ENC_KEY", "").strip()
    if not key:
        raise SecretBoxUnavailable("TOKEN_ENC_KEY absent")
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as e:
        raise SecretBoxUnavailable("TOKEN_ENC_KEY invalide") from e


def is_encrypted(value: str | None) -> bool:
    return bool(value) and value.startswith(PREFIX)


def encrypt_secret(plain: str) -> str:
    return PREFIX + _fernet().encrypt(plain.encode()).decode()


def decrypt_secret(stored: str | None) -> str | None:
    """Clair d'un secret chiffré ; une ancienne valeur en clair est rendue telle quelle (migration)."""
    if not stored:
        return None
    if not is_encrypted(stored):
        return stored
    try:
        return _fernet().decrypt(stored[len(PREFIX):].encode()).decode()
    except InvalidToken:
        return None
