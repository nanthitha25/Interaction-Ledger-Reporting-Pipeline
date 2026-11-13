"""
PII Vault Abstraction for Interaction Ledger & Reporting Pipeline.

Provides high-level interfaces for encrypting, decrypting, blind-indexing,
and masking PII attributes (phone numbers and customer names), as well as
scrubbing PII from event payloads before database persistence.
Plaintext PII is strictly quarantined from disk storage, database payloads, and diagnostic logs.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from src.config import SecurityConfig, load_config
from src.ingestion.normalizers import InvalidPhoneError, normalize_indian_phone
from src.security.crypto import (
    CryptoError,
    DecryptionError,
    EncryptionError,
    InvalidCiphertextError,
    InvalidKeyError,
    compute_hmac_sha256_index,
    decrypt_aes_gcm,
    encrypt_aes_gcm,
)

logger = logging.getLogger(__name__)

# Normalized PII key names (lower-case alphanumeric with whitespace, underscores, and hyphens stripped)
PII_NORMALIZED_KEYS = frozenset(
    {
        # Phone number variations
        "phone",
        "phonenumber",
        "customerphone",
        "callerphone",
        "userphone",
        "mobile",
        "mobilenumber",
        "contactnumber",
        "contact",
        "telephone",
        "tel",
        # Customer name variations
        "customername",
        "name",
        "callername",
        "fullname",
        "clientname",
        "username",
    }
)


def _is_pii_key(key: Any) -> bool:
    """Checks whether a dictionary key corresponds to a known PII attribute."""
    if not isinstance(key, str):
        return False
    normalized = re.sub(r"[\s_-]+", "", key.lower())
    return normalized in PII_NORMALIZED_KEYS


def _scrub_recursive(item: Any) -> Any:
    """Recursively removes PII keys from nested dictionaries and lists."""
    if isinstance(item, dict):
        cleaned = {}
        for k, v in item.items():
            if _is_pii_key(k):
                continue
            cleaned[k] = _scrub_recursive(v)
        return cleaned
    elif isinstance(item, list):
        return [_scrub_recursive(elem) for elem in item]
    return item


def _format_context(field_tag: str, context: Optional[str | bytes] = None) -> bytes:
    """
    Constructs Authenticated Associated Data (AAD) for AES-GCM context binding.
    Prevents ciphertext transplant attacks between different fields or records.
    """
    base = f"field:{field_tag}".encode("utf-8")
    if context is None:
        return base
    if isinstance(context, str):
        ctx_bytes = context.encode("utf-8")
    else:
        ctx_bytes = bytes(context)
    return base + b":" + ctx_bytes


class PIIVault:
    """
    High-level PII Vault abstraction enforcing cryptographic isolation of PII at rest.
    """

    def __init__(self, config: Optional[SecurityConfig] = None):
        """
        Initializes the PII Vault with cryptographic keys from configuration.

        Args:
            config: Optional SecurityConfig containing HMAC and AES keys.
                    If omitted, loaded from application environment configuration.
        """
        if config is None:
            app_config = load_config()
            config = app_config.security

        self._hmac_key = config.hmac_secret_key
        self._aes_key = config.aes_encryption_key

        # Enforce key separation of duties: HMAC key and AES key must never be identical byte-for-byte
        if self._hmac_key == self._aes_key:
            raise InvalidKeyError(
                "Cryptographic separation failure: HMAC secret key and AES encryption key must be distinct."
            )

    @property
    def hmac_key(self) -> bytes:
        """Returns the HMAC secret key."""
        return self._hmac_key

    @property
    def aes_key(self) -> bytes:
        """Returns the AES encryption key."""
        return self._aes_key

    def get_phone_blind_index(self, phone: str) -> str:
        """
        Computes the deterministic HMAC-SHA256 blind index for a phone number.
        The input phone is canonicalized to '+91XXXXXXXXXX' prior to hashing.

        Args:
            phone: Raw or canonical phone number.

        Returns:
            64-character lowercase hexadecimal string matching PostgreSQL CHAR(64).

        Raises:
            InvalidPhoneError: If the phone number fails canonical validation.
            CryptoError: If blind index calculation fails.
        """
        try:
            canonical_phone = normalize_indian_phone(phone)
            return compute_hmac_sha256_index(canonical_phone, self._hmac_key)
        except InvalidPhoneError:
            raise
        except Exception:
            raise CryptoError("Failed to compute phone blind index.") from None

    def encrypt_phone(self, phone: str, context: Optional[str | bytes] = None) -> str:
        """
        Canonicalizes and encrypts a phone number using AES-256-GCM.

        Args:
            phone: Raw or canonical phone number.
            context: Optional domain context string or bytes for AEAD binding.

        Returns:
            Serialized ciphertext string in format 'v1:<base64>'.

        Raises:
            InvalidPhoneError: If the phone number fails canonical validation.
            EncryptionError: If encryption fails.
        """
        try:
            canonical_phone = normalize_indian_phone(phone)
            aad = _format_context("phone", context)
            return encrypt_aes_gcm(canonical_phone, self._aes_key, associated_data=aad)
        except InvalidPhoneError:
            raise
        except Exception:
            raise EncryptionError("Failed to encrypt phone number.") from None

    def decrypt_phone(self, phone_encrypted: str, context: Optional[str | bytes] = None) -> str:
        """
        Decrypts an encrypted phone ciphertext and returns the canonical phone number.

        Args:
            phone_encrypted: Serialized ciphertext string.
            context: Optional domain context string or bytes that was used during encryption.

        Returns:
            Canonical phone string '+91XXXXXXXXXX'.

        Raises:
            DecryptionError: If decryption or authentication tag check fails.
            InvalidCiphertextError: If ciphertext format is malformed.
        """
        try:
            aad = _format_context("phone", context)
            return decrypt_aes_gcm(phone_encrypted, self._aes_key, associated_data=aad)
        except (DecryptionError, InvalidCiphertextError):
            raise
        except Exception:
            raise DecryptionError("Failed to decrypt phone number.") from None

    def mask_phone(self, phone: str) -> str:
        """
        Returns the standard masked representation of a phone number: '+91******XXXX'.
        Only the last 4 digits are exposed; the preceding digits are masked.

        Args:
            phone: Raw or canonical phone number.

        Returns:
            Masked phone string '+91******XXXX'.

        Raises:
            InvalidPhoneError: If the phone cannot be canonicalized.
        """
        canonical_phone = normalize_indian_phone(phone)
        return f"+91******{canonical_phone[-4:]}"

    def encrypt_customer_name(self, name: str, context: Optional[str | bytes] = None) -> str:
        """
        Encrypts a customer name using AES-256-GCM.

        Args:
            name: Customer name string.
            context: Optional domain context string or bytes for AEAD binding.

        Returns:
            Serialized ciphertext string.

        Raises:
            ValueError: If the customer name is empty.
            EncryptionError: If encryption fails.
        """
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Customer name cannot be empty.")
        trimmed_name = name.strip()
        try:
            aad = _format_context("customer_name", context)
            return encrypt_aes_gcm(trimmed_name, self._aes_key, associated_data=aad)
        except Exception:
            raise EncryptionError("Failed to encrypt customer name.") from None

    def decrypt_customer_name(self, name_encrypted: str, context: Optional[str | bytes] = None) -> str:
        """
        Decrypts an encrypted customer name ciphertext.

        Args:
            name_encrypted: Serialized ciphertext string.
            context: Optional domain context string or bytes that was used during encryption.

        Returns:
            Decrypted customer name string.

        Raises:
            DecryptionError: If decryption or tag verification fails.
            InvalidCiphertextError: If ciphertext format is malformed.
        """
        try:
            aad = _format_context("customer_name", context)
            return decrypt_aes_gcm(name_encrypted, self._aes_key, associated_data=aad)
        except (DecryptionError, InvalidCiphertextError):
            raise
        except Exception:
            raise DecryptionError("Failed to decrypt customer name.") from None

    def scrub_payload_pii(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        Recursively scrubs plaintext PII attributes from an event payload dictionary
        before persistence into interaction_ledger.payload_json.

        All variations of phone and customer name keys (case-insensitive, normalized)
        are recursively purged from top-level and nested structures, while all non-PII
        domain attributes are preserved intact.

        Args:
            payload: Input event payload dictionary.

        Returns:
            New dictionary containing only non-PII domain payload attributes.
        """
        if not isinstance(payload, dict):
            return {}

        return _scrub_recursive(payload)
