"""
Unit tests for PII Vault abstraction and security boundaries.
"""

import base64
import pytest

from src.config import SecurityConfig
from src.ingestion.normalizers import InvalidPhoneError
from src.security.crypto import DecryptionError, InvalidKeyError
from src.security.vault import PIIVault

HMAC_KEY = b"vault_hmac_secret_32_bytes_pad_0"
AES_KEY = b"vault_aes_encrypt_32_bytes_pad_1"


@pytest.fixture
def vault():
    config = SecurityConfig(
        hmac_secret_key=HMAC_KEY,
        aes_encryption_key=AES_KEY,
    )
    return PIIVault(config=config)


def test_vault_key_separation_enforced():
    # If HMAC key and AES key are identical, vault must refuse to initialize
    same_key = b"identical_key_for_both_000000000"
    config = SecurityConfig(hmac_secret_key=same_key, aes_encryption_key=same_key)
    with pytest.raises(InvalidKeyError, match="Cryptographic separation failure"):
        PIIVault(config=config)


def test_phone_encryption_and_decryption_round_trip(vault):
    raw_phone = "9876543210"
    encrypted = vault.encrypt_phone(raw_phone)
    assert encrypted.startswith("v1:")
    decrypted = vault.decrypt_phone(encrypted)
    # Automatically canonicalized to +91
    assert decrypted == "+919876543210"


def test_phone_blind_index_deterministic_canonicalization(vault):
    # Formats should all resolve to the same canonical +919876543210 and produce the same index
    idx1 = vault.get_phone_blind_index("9876543210")
    idx2 = vault.get_phone_blind_index("+91 98765 43210")
    idx3 = vault.get_phone_blind_index("09876543210")
    idx4 = vault.get_phone_blind_index("91-9876543210")
    assert idx1 == idx2 == idx3 == idx4
    assert len(idx1) == 64


def test_phone_masking_standard_format(vault):
    masked = vault.mask_phone("9876543210")
    assert masked == "+91******3210"
    assert "9876" not in masked


def test_invalid_phone_rejected_without_leaking_pii(vault):
    secret_phone = "12345"
    with pytest.raises(InvalidPhoneError) as exc_info:
        vault.encrypt_phone(secret_phone)
    # Ensure plaintext phone is NOT leaked in exception message
    assert secret_phone not in str(exc_info.value)

    with pytest.raises(InvalidPhoneError) as exc_info:
        vault.get_phone_blind_index(secret_phone)
    assert secret_phone not in str(exc_info.value)


def test_customer_name_encryption_and_decryption(vault):
    name = "Ramesh Kumar"
    encrypted = vault.encrypt_customer_name(name)
    assert encrypted.startswith("v1:")
    decrypted = vault.decrypt_customer_name(encrypted)
    assert decrypted == "Ramesh Kumar"


def test_customer_name_unicode_support(vault):
    unicode_name = "தமிழ்ச்செல்வன் / राजेश शर्मा"
    encrypted = vault.encrypt_customer_name(unicode_name)
    assert vault.decrypt_customer_name(encrypted) == unicode_name


def test_empty_customer_name_rejected(vault):
    with pytest.raises(ValueError, match="Customer name cannot be empty"):
        vault.encrypt_customer_name("   ")


def test_keys_not_interchangeable():
    # Ciphertext encrypted with AES_KEY cannot be decrypted using HMAC_KEY
    config1 = SecurityConfig(hmac_secret_key=HMAC_KEY, aes_encryption_key=AES_KEY)
    config2 = SecurityConfig(hmac_secret_key=AES_KEY, aes_encryption_key=HMAC_KEY)

    vault1 = PIIVault(config=config1)
    vault2 = PIIVault(config=config2)

    ct = vault1.encrypt_phone("9876543210")
    with pytest.raises(DecryptionError):
        vault2.decrypt_phone(ct)


def test_scrub_payload_pii(vault):
    raw_payload = {
        "call_id": "call_12345",
        "phone": "+919876543210",
        "Phone": "09876543210",
        "customer_name": "John Doe",
        "reason": "carrier_rejected",
        "duration_sec": 45,
    }
    scrubbed = vault.scrub_payload_pii(raw_payload)

    # PII fields must be removed
    assert "phone" not in scrubbed
    assert "Phone" not in scrubbed
    assert "customer_name" not in scrubbed

    # Domain attributes must be strictly preserved
    assert scrubbed["call_id"] == "call_12345"
    assert scrubbed["reason"] == "carrier_rejected"
    assert scrubbed["duration_sec"] == 45


def test_no_plaintext_pii_in_decryption_errors(vault):
    tampered_ct = "v1:" + "A" * 40
    with pytest.raises(DecryptionError) as exc_info:
        vault.decrypt_phone(tampered_ct)
    # Ensure error is generic and does not leak keys or internal sensitive data
    err_msg = str(exc_info.value)
    assert "key" not in err_msg.lower() or "verification failed" in err_msg.lower()
    assert "9876543210" not in err_msg


def test_key_separation_detects_different_encodings_of_same_bytes():
    # Raw ASCII string vs Base64 encoded string that resolve to identical 32 bytes
    raw_key = b"shared_secret_key_32_bytes_pad01"
    raw_str = raw_key.decode("ascii")
    b64_str = base64.b64encode(raw_key).decode("ascii")

    from src.config import load_config
    config = load_config(env_override={
        "HMAC_SECRET_KEY": raw_str,
        "AES_ENCRYPTION_KEY": b64_str,
    })

    with pytest.raises(InvalidKeyError, match="Cryptographic separation failure"):
        PIIVault(config=config.security)


def test_nested_and_alternative_pii_keys_scrubbed(vault):
    payload = {
        "call_id": "call_999",
        "CALLER_PHONE": "+919876543210",
        "Mobile_Number": "9876543210",
        "Contact-Number": "09876543210",
        "Full_Name": "Priya Sharma",
        "Client-Name": "Acme Corp",
        "duration_sec": 120,
        "reason": "carrier_rejected",
        "nested_meta": {
            "customer_phone": "919876543210",
            "caller_name": "Priya",
            "workflow_code": "WRK_01",
            "retry_count": 2,
        },
        "interaction_log": [
            {"phone": "+919123456789", "status": "dialed"},
            {"user_name": "admin_agent", "action": "override_approved"},
        ],
    }

    scrubbed = vault.scrub_payload_pii(payload)

    # Top-level PII removed
    for pii_key in ("CALLER_PHONE", "Mobile_Number", "Contact-Number", "Full_Name", "Client-Name"):
        assert pii_key not in scrubbed

    # Top-level domain attributes preserved
    assert scrubbed["call_id"] == "call_999"
    assert scrubbed["duration_sec"] == 120
    assert scrubbed["reason"] == "carrier_rejected"

    # Nested dictionary scrubbed while non-PII preserved
    assert "customer_phone" not in scrubbed["nested_meta"]
    assert "caller_name" not in scrubbed["nested_meta"]
    assert scrubbed["nested_meta"]["workflow_code"] == "WRK_01"
    assert scrubbed["nested_meta"]["retry_count"] == 2

    # Nested list of dicts scrubbed
    assert "phone" not in scrubbed["interaction_log"][0]
    assert scrubbed["interaction_log"][0]["status"] == "dialed"
    assert "user_name" not in scrubbed["interaction_log"][1]
    assert scrubbed["interaction_log"][1]["action"] == "override_approved"


def test_context_binding_prevents_field_transplant(vault):
    # Ciphertext encrypted as phone cannot be decrypted as customer name
    phone = "9876543210"
    phone_ct = vault.encrypt_phone(phone)

    with pytest.raises(DecryptionError):
        vault.decrypt_customer_name(phone_ct)

    # Ciphertext encrypted as customer name cannot be decrypted as phone
    name = "Rajesh Gupta"
    name_ct = vault.encrypt_customer_name(name)

    with pytest.raises(DecryptionError):
        vault.decrypt_phone(name_ct)


def test_explicit_domain_context_binding(vault):
    phone = "9876543210"

    # Encrypt with workflow context
    ct_motor = vault.encrypt_phone(phone, context="motor_renewal")

    # Decrypt with matching context succeeds
    assert vault.decrypt_phone(ct_motor, context="motor_renewal") == "+919876543210"

    # Decrypt with mismatched context fails authentication
    with pytest.raises(DecryptionError):
        vault.decrypt_phone(ct_motor, context="policy_feedback")

    # Decrypt with missing context fails authentication
    with pytest.raises(DecryptionError):
        vault.decrypt_phone(ct_motor, context=None)


def test_logging_and_exception_safety_with_caplog(vault, caplog):
    import logging
    caplog.set_level(logging.DEBUG)

    sensitive_phone = "12345"  # invalid phone
    sensitive_name = "Secret Customer"

    # 1. Invalid phone error message does not contain the phone
    with pytest.raises(InvalidPhoneError) as exc_info:
        vault.encrypt_phone(sensitive_phone)
    assert sensitive_phone not in str(exc_info.value)

    # 2. Tampered ciphertext decryption error does not contain keys or internal bytes
    with pytest.raises(DecryptionError) as exc_info:
        vault.decrypt_phone("v1:" + "Z" * 44)
    assert "vault_aes_encrypt" not in str(exc_info.value)

    # 3. Verify application logs captured during operations have zero sensitive data
    assert sensitive_phone not in caplog.text
    assert sensitive_name not in caplog.text
    assert "vault_hmac_secret" not in caplog.text
    assert "vault_aes_encrypt" not in caplog.text

