"""
Cryptographic Engine for AES-256-GCM and HMAC-SHA256 Blind Indexing.

Implements authenticated encryption with associated data (AEAD) using AES-256-GCM,
cryptographically secure 12-byte random nonces, and deterministic HMAC-SHA256 blind indexing.
Zero plaintext PII or cryptographic keys are ever logged or exposed in exceptions.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from typing import Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Constants
AES_KEY_LENGTH_BYTES = 32
AES_NONCE_LENGTH_BYTES = 12
AES_TAG_LENGTH_BYTES = 16
CIPHERTEXT_VERSION_PREFIX = "v1:"
HMAC_MIN_KEY_LENGTH_BYTES = 32


class CryptoError(Exception):
    """Base exception for cryptographic operations."""


class InvalidKeyError(CryptoError):
    """Raised when an encryption or HMAC key is invalid, wrong length, or corrupted."""


class EncryptionError(CryptoError):
    """Raised when encryption operation fails."""


class DecryptionError(CryptoError):
    """Raised when decryption or authentication tag verification fails."""


class InvalidCiphertextError(DecryptionError):
    """Raised when ciphertext payload is malformed or improperly formatted."""


def encrypt_aes_gcm(
    plaintext: str | bytes,
    key: bytes,
    associated_data: Optional[bytes] = None,
) -> str:
    """
    Encrypts plaintext using AES-256-GCM with a cryptographically secure 12-byte random nonce.

    Args:
        plaintext: The string or bytes to encrypt.
        key: Exactly 32 bytes AES-256 key.
        associated_data: Optional authenticated associated data (AAD).

    Returns:
        Serialized ciphertext string in format: 'v1:<base64(nonce + ciphertext + tag)>'.

    Raises:
        InvalidKeyError: If the key length is not exactly 32 bytes.
        EncryptionError: If the encryption operation fails.
    """
    if not isinstance(key, (bytes, bytearray)):
        raise InvalidKeyError("AES key must be bytes.")
    if len(key) != AES_KEY_LENGTH_BYTES:
        raise InvalidKeyError(
            f"AES-256 key must be exactly {AES_KEY_LENGTH_BYTES} bytes (got {len(key)} bytes)."
        )

    if isinstance(plaintext, str):
        data_bytes = plaintext.encode("utf-8")
    elif isinstance(plaintext, (bytes, bytearray)):
        data_bytes = bytes(plaintext)
    else:
        raise EncryptionError("Plaintext must be str or bytes.")

    nonce = os.urandom(AES_NONCE_LENGTH_BYTES)
    try:
        aesgcm = AESGCM(key)
        ciphertext_and_tag = aesgcm.encrypt(nonce, data_bytes, associated_data)
    except Exception as e:
        raise EncryptionError("Encryption operation failed.") from e

    # Package: nonce (12 bytes) + ciphertext_and_tag (N bytes + 16 bytes tag)
    payload_bytes = nonce + ciphertext_and_tag
    b64_str = base64.b64encode(payload_bytes).decode("ascii")
    return f"{CIPHERTEXT_VERSION_PREFIX}{b64_str}"


def decrypt_aes_gcm(
    serialized_ciphertext: str,
    key: bytes,
    associated_data: Optional[bytes] = None,
) -> str:
    """
    Decrypts and authenticates an AES-256-GCM serialized ciphertext string.

    Args:
        serialized_ciphertext: Ciphertext in format 'v1:<base64(nonce + ciphertext + tag)>'.
        key: Exactly 32 bytes AES-256 key.
        associated_data: Optional authenticated associated data (AAD).

    Returns:
        Decrypted plaintext as a UTF-8 string.

    Raises:
        InvalidKeyError: If the key length is not exactly 32 bytes.
        InvalidCiphertextError: If the format is invalid, missing prefix, or truncated.
        DecryptionError: If the authentication tag verification fails, ciphertext is tampered,
                         or the decryption key is incorrect.
    """
    if not isinstance(key, (bytes, bytearray)):
        raise InvalidKeyError("AES key must be bytes.")
    if len(key) != AES_KEY_LENGTH_BYTES:
        raise InvalidKeyError(
            f"AES-256 key must be exactly {AES_KEY_LENGTH_BYTES} bytes (got {len(key)} bytes)."
        )

    if not isinstance(serialized_ciphertext, str) or not serialized_ciphertext.startswith(
        CIPHERTEXT_VERSION_PREFIX
    ):
        raise InvalidCiphertextError(
            f"Invalid ciphertext format: missing expected '{CIPHERTEXT_VERSION_PREFIX}' version prefix."
        )

    b64_part = serialized_ciphertext[len(CIPHERTEXT_VERSION_PREFIX) :]
    try:
        payload_bytes = base64.b64decode(b64_part, validate=True)
    except Exception as e:
        raise InvalidCiphertextError("Malformed ciphertext: invalid base64 encoding.") from e

    min_required_len = AES_NONCE_LENGTH_BYTES + AES_TAG_LENGTH_BYTES
    if len(payload_bytes) < min_required_len:
        raise InvalidCiphertextError(
            f"Malformed ciphertext: payload is truncated (minimum {min_required_len} bytes required)."
        )

    nonce = payload_bytes[:AES_NONCE_LENGTH_BYTES]
    ciphertext_and_tag = payload_bytes[AES_NONCE_LENGTH_BYTES:]

    try:
        aesgcm = AESGCM(key)
        decrypted_bytes = aesgcm.decrypt(nonce, ciphertext_and_tag, associated_data)
        return decrypted_bytes.decode("utf-8")
    except InvalidTag as e:
        raise DecryptionError(
            "Authentication tag verification failed: ciphertext is tampered, tag is corrupted, or key is incorrect."
        ) from e
    except Exception as e:
        raise DecryptionError("Decryption failed.") from e


def compute_hmac_sha256_index(value: str, key: bytes) -> str:
    """
    Generates a deterministic HMAC-SHA256 blind index for equality lookup.

    Args:
        value: Input string (e.g. canonical phone number).
        key: Secret HMAC key of at least 32 bytes.

    Returns:
        64-character lowercase hexadecimal digest string.

    Raises:
        InvalidKeyError: If the HMAC key length is less than 32 bytes.
        CryptoError: If the input value is empty or not a string.
    """
    if not isinstance(key, (bytes, bytearray)):
        raise InvalidKeyError("HMAC key must be bytes.")
    if len(key) < HMAC_MIN_KEY_LENGTH_BYTES:
        raise InvalidKeyError(
            f"HMAC secret key must be at least {HMAC_MIN_KEY_LENGTH_BYTES} bytes (got {len(key)} bytes)."
        )

    if not isinstance(value, str) or not value.strip():
        raise CryptoError("Cannot compute blind index for empty or non-string value.")

    mac = hmac.new(key, value.strip().encode("utf-8"), hashlib.sha256)
    return mac.hexdigest().lower()
