"""
Unit tests for application configuration and environment secret validation.
"""

import base64
import pytest
from src.config import ConfigurationError, load_config


def test_default_config_loads_cleanly():
    config = load_config(env_override={})
    assert config.environment == "development"
    assert "postgresql://" in config.db.url
    assert len(config.security.hmac_secret_key) == 32
    assert len(config.security.aes_encryption_key) == 32


def test_custom_database_url():
    config = load_config(env_override={"DATABASE_URL": "postgresql://testuser:testpass@localhost:5432/custom_db"})
    assert config.db.url == "postgresql://testuser:testpass@localhost:5432/custom_db"


def test_postgres_scheme_accepted():
    config = load_config(env_override={"DATABASE_URL": "postgres://localhost:5432/db"})
    assert config.db.url == "postgres://localhost:5432/db"


def test_invalid_database_url_scheme_rejected():
    with pytest.raises(ConfigurationError, match="Invalid DATABASE_URL scheme 'mysql'"):
        load_config(env_override={"DATABASE_URL": "mysql://localhost:3306/db"})


def test_empty_database_url_rejected():
    with pytest.raises(ConfigurationError, match="DATABASE_URL must not be empty"):
        load_config(env_override={"DATABASE_URL": ""})


def test_missing_hostname_rejected():
    with pytest.raises(ConfigurationError, match="missing a valid hostname"):
        load_config(env_override={"DATABASE_URL": "postgresql:///mydb"})


def test_production_environment_fails_closed_when_secrets_missing():
    with pytest.raises(ConfigurationError, match="Missing required secret 'HMAC_SECRET_KEY' in production"):
        load_config(env_override={"ENVIRONMENT": "production", "HMAC_SECRET_KEY": "", "AES_ENCRYPTION_KEY": ""})


def test_short_secret_rejected():
    with pytest.raises(ConfigurationError, match="insufficient length"):
        load_config(env_override={"HMAC_SECRET_KEY": "short_secret"})


def test_valid_32_byte_raw_secret():
    valid_key = "a" * 32
    config = load_config(env_override={"HMAC_SECRET_KEY": valid_key})
    assert config.security.hmac_secret_key == valid_key.encode("utf-8")


def test_valid_base64_encoded_32_byte_secret():
    raw = b"12345678901234567890123456789012"
    b64 = base64.b64encode(raw).decode("utf-8")
    config = load_config(env_override={"AES_ENCRYPTION_KEY": b64})
    assert config.security.aes_encryption_key == raw


def test_invalid_aes_key_length_rejected():
    with pytest.raises(ConfigurationError, match="exactly 32 bytes"):
        load_config(env_override={"AES_ENCRYPTION_KEY": "a" * 40})

    with pytest.raises(ConfigurationError, match="exactly 32 bytes"):
        load_config(env_override={"AES_ENCRYPTION_KEY": "a" * 16})


def test_invalid_connection_pool_bounds():
    with pytest.raises(ConfigurationError, match="Invalid connection pool bounds"):
        load_config(env_override={"DB_MIN_CONNECTIONS": "10", "DB_MAX_CONNECTIONS": "2"})
