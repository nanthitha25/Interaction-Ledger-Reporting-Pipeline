"""
Unit tests for AES-256-GCM and HMAC-SHA256 Cryptographic Engine.
"""

import base64
import os
import pytest

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

SAMPLE_KEY_32 = b"12345678901234567890123456789012"
ANOTHER_KEY_32 = b"abcdefghijklmnopqrstuvwxyz012345"
SAMPLE_HMAC_KEY = b"hmac_secret_key_32_bytes_pad_001"
ANOTHER_HMAC_KEY = b"hmac_secret_key_32_bytes_pad_002"


def test_aes_round_trip():
    plaintext = "Sensitive Customer Data 12345"
    ciphertext = encrypt_aes_gcm(plaintext, SAMPLE_KEY_32)
    assert ciphertext.startswith("v1:")
    decrypted = decrypt_aes_gcm(ciphertext, SAMPLE_KEY_32)
    assert decrypted == plaintext


def test_random_nonce_produces_different_ciphertexts():
    plaintext = "+919876543210"
    ct1 = encrypt_aes_gcm(plaintext, SAMPLE_KEY_32)
    ct2 = encrypt_aes_gcm(plaintext, SAMPLE_KEY_32)
    assert ct1 != ct2
    assert decrypt_aes_gcm(ct1, SAMPLE_KEY_32) == plaintext
    assert decrypt_aes_gcm(ct2, SAMPLE_KEY_32) == plaintext


def test_decryption_with_correct_key():
    plaintext = "Aadhaar / PII Data"
    ct = encrypt_aes_gcm(plaintext, SAMPLE_KEY_32)
    assert decrypt_aes_gcm(ct, SAMPLE_KEY_32) == plaintext


def test_decryption_fails_with_incorrect_key():
    plaintext = "+919876543210"
    ct = encrypt_aes_gcm(plaintext, SAMPLE_KEY_32)
    with pytest.raises(DecryptionError, match="Authentication tag verification failed"):
        decrypt_aes_gcm(ct, ANOTHER_KEY_32)


def test_tampered_ciphertext_rejected():
    plaintext = "+919876543210"
    ct = encrypt_aes_gcm(plaintext, SAMPLE_KEY_32)
    b64_part = ct[3:]
    raw_payload = bytearray(base64.b64decode(b64_part))

    # Tamper byte in ciphertext region (after 12-byte nonce)
    raw_payload[15] ^= 0xFF
    tampered_ct = f"v1:{base64.b64encode(raw_payload).decode('ascii')}"

    with pytest.raises(DecryptionError, match="Authentication tag verification failed"):
        decrypt_aes_gcm(tampered_ct, SAMPLE_KEY_32)


def test_tampered_tag_rejected():
    plaintext = "+919876543210"
    ct = encrypt_aes_gcm(plaintext, SAMPLE_KEY_32)
    b64_part = ct[3:]
    raw_payload = bytearray(base64.b64decode(b64_part))

    # Tamper the very last byte (within the 16-byte authentication tag)
    raw_payload[-1] ^= 0x01
    tampered_ct = f"v1:{base64.b64encode(raw_payload).decode('ascii')}"

    with pytest.raises(DecryptionError, match="Authentication tag verification failed"):
        decrypt_aes_gcm(tampered_ct, SAMPLE_KEY_32)


def test_invalid_ciphertext_format_rejected():
    # Missing version prefix
    with pytest.raises(InvalidCiphertextError, match="missing expected 'v1:' version prefix"):
        decrypt_aes_gcm("not_valid_format", SAMPLE_KEY_32)

    # Invalid base64 encoding
    with pytest.raises(InvalidCiphertextError, match="invalid base64 encoding"):
        decrypt_aes_gcm("v1:???invalid_base64!!!", SAMPLE_KEY_32)

    # Truncated payload (< 28 bytes)
    truncated_b64 = base64.b64encode(b"short").decode("ascii")
    with pytest.raises(InvalidCiphertextError, match="payload is truncated"):
        decrypt_aes_gcm(f"v1:{truncated_b64}", SAMPLE_KEY_32)


def test_aes_keys_of_invalid_lengths_rejected():
    for invalid_len in (0, 16, 24, 31, 33, 64):
        bad_key = b"x" * invalid_len
        with pytest.raises(InvalidKeyError, match="AES-256 key must be exactly 32 bytes"):
            encrypt_aes_gcm("test", bad_key)

        with pytest.raises(InvalidKeyError, match="AES-256 key must be exactly 32 bytes"):
            decrypt_aes_gcm("v1:valid_placeholder", bad_key)


def test_hmac_blind_index_deterministic():
    phone = "+919876543210"
    idx1 = compute_hmac_sha256_index(phone, SAMPLE_HMAC_KEY)
    idx2 = compute_hmac_sha256_index(phone, SAMPLE_HMAC_KEY)
    assert idx1 == idx2
    assert len(idx1) == 64
    assert idx1.lower() == idx1


def test_different_phones_produce_different_blind_indexes():
    phone1 = "+919876543210"
    phone2 = "+919876543211"
    idx1 = compute_hmac_sha256_index(phone1, SAMPLE_HMAC_KEY)
    idx2 = compute_hmac_sha256_index(phone2, SAMPLE_HMAC_KEY)
    assert idx1 != idx2


def test_different_hmac_keys_produce_different_blind_indexes():
    phone = "+919876543210"
    idx1 = compute_hmac_sha256_index(phone, SAMPLE_HMAC_KEY)
    idx2 = compute_hmac_sha256_index(phone, ANOTHER_HMAC_KEY)
    assert idx1 != idx2


def test_hmac_short_key_rejected():
    with pytest.raises(InvalidKeyError, match="must be at least 32 bytes"):
        compute_hmac_sha256_index("+919876543210", b"short_key")


def test_empty_value_blind_index_rejected():
    with pytest.raises(CryptoError, match="empty or non-string value"):
        compute_hmac_sha256_index("   ", SAMPLE_HMAC_KEY)


def test_associated_data_protection():
    plaintext = "Order Reference REF123"
    aad = b"context=tenant_101"
    wrong_aad = b"context=tenant_102"

    ct = encrypt_aes_gcm(plaintext, SAMPLE_KEY_32, associated_data=aad)
    # Decrypt with correct AAD
    assert decrypt_aes_gcm(ct, SAMPLE_KEY_32, associated_data=aad) == plaintext
    # Decrypt with mismatched AAD fails authentication
    with pytest.raises(DecryptionError, match="Authentication tag verification failed"):
        decrypt_aes_gcm(ct, SAMPLE_KEY_32, associated_data=wrong_aad)


def test_never_fall_back_to_plaintext_on_tamper():
    plaintext = "+919999999999"
    ct = encrypt_aes_gcm(plaintext, SAMPLE_KEY_32)
    # Corrupting payload
    corrupted = ct[:10] + "Z" + ct[11:]
    with pytest.raises(DecryptionError):
        res = decrypt_aes_gcm(corrupted, SAMPLE_KEY_32)
        assert plaintext not in res
