"""
Configuration and Environment Settings for the Interaction Ledger & Reporting Pipeline.

Enforces fail-closed validation of secrets and database connection parameters.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from dotenv import load_dotenv

# Load .env if present
load_dotenv()


class ConfigurationError(Exception):
    """Raised when application configuration or secrets are invalid or missing."""


@dataclass(frozen=True)
class DatabaseConfig:
    url: str
    min_connections: int = 1
    max_connections: int = 10


@dataclass(frozen=True)
class SecurityConfig:
    hmac_secret_key: bytes
    aes_encryption_key: bytes


@dataclass(frozen=True)
class AppConfig:
    environment: str
    db: DatabaseConfig
    security: SecurityConfig


def _validate_database_url(url: str) -> None:
    if not url:
        raise ConfigurationError("DATABASE_URL must not be empty.")
    parsed = urlparse(url)
    if parsed.scheme not in ("postgresql", "postgres"):
        raise ConfigurationError(
            f"Invalid DATABASE_URL scheme '{parsed.scheme}'. Must be 'postgresql://' or 'postgres://'."
        )
    if not parsed.hostname:
        raise ConfigurationError(f"DATABASE_URL '{url}' is missing a valid hostname.")


def _parse_and_validate_key(
    key_str: Optional[str],
    key_name: str,
    required_bytes: int = 32,
    exact_length: bool = False,
    environment: str = "development",
    fallback_default: Optional[bytes] = None,
) -> bytes:
    if not key_str:
        if environment == "production":
            raise ConfigurationError(f"Missing required secret '{key_name}' in production environment.")
        if fallback_default is not None:
            return fallback_default
        raise ConfigurationError(f"Missing required secret '{key_name}'.")

    raw_bytes = key_str.encode("utf-8")

    # If key_str is 43+ chars, evaluate if it is base64-encoded binary key (32 bytes is 44 chars padded or 43 unpadded)
    if len(key_str) >= 43:
        try:
            decoded = base64.b64decode(key_str, validate=True)
            if exact_length:
                if len(decoded) == required_bytes:
                    return decoded
            else:
                if len(decoded) >= required_bytes:
                    return decoded
        except Exception:
            pass

    # Raw bytes validation
    if exact_length:
        if len(raw_bytes) != required_bytes:
            raise ConfigurationError(
                f"Secret '{key_name}' must be exactly {required_bytes} bytes (got {len(raw_bytes)} bytes)."
            )
        return raw_bytes

    if len(raw_bytes) < required_bytes:
        raise ConfigurationError(
            f"Secret '{key_name}' has insufficient length ({len(raw_bytes)} bytes). "
            f"Must be at least {required_bytes} bytes."
        )
    return raw_bytes


def load_config(env_override: Optional[dict[str, str]] = None) -> AppConfig:
    """
    Loads application configuration from environment variables or supplied dictionary.
    Enforces fail-closed validation on all critical security and connection parameters.
    """
    env = dict(os.environ)
    if env_override:
        env.update(env_override)

    environment = env.get("APP_ENV", env.get("ENVIRONMENT", "development")).lower()

    # Database configuration
    db_url = env.get(
        "DATABASE_URL",
        "postgresql://localhost:5432/interaction_ledger_test",
    )
    _validate_database_url(db_url)

    try:
        min_conn = int(env.get("DB_MIN_CONNECTIONS", "1"))
        max_conn = int(env.get("DB_MAX_CONNECTIONS", "10"))
    except ValueError as e:
        raise ConfigurationError(f"Invalid connection pool limits: {e}") from e

    if min_conn < 1 or max_conn < min_conn:
        raise ConfigurationError(f"Invalid connection pool bounds: min={min_conn}, max={max_conn}")

    db_config = DatabaseConfig(
        url=db_url,
        min_connections=min_conn,
        max_connections=max_conn,
    )

    # Security configuration
    # Dev/test defaults with exactly 32 bytes of deterministic pseudorandom data
    dev_hmac_default = b"dev_hmac_secret_key_32_bytes_pad"
    dev_aes_default = b"dev_aes_encryption_key_32_bytes!"

    hmac_key = _parse_and_validate_key(
        env.get("HMAC_SECRET_KEY"),
        "HMAC_SECRET_KEY",
        required_bytes=32,
        exact_length=False,
        environment=environment,
        fallback_default=dev_hmac_default,
    )

    aes_key = _parse_and_validate_key(
        env.get("AES_ENCRYPTION_KEY"),
        "AES_ENCRYPTION_KEY",
        required_bytes=32,
        exact_length=True,
        environment=environment,
        fallback_default=dev_aes_default,
    )

    security_config = SecurityConfig(
        hmac_secret_key=hmac_key,
        aes_encryption_key=aes_key,
    )

    return AppConfig(
        environment=environment,
        db=db_config,
        security=security_config,
    )
