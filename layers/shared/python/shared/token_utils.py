"""Token Utilities — HMAC-based token generation and validation for subscriptions.

Generates signed tokens for:
- Email verification (double opt-in)
- Unsubscribe links (one-click unsubscribe)
- API authentication (status checks)

Tokens encode: county_fips + subscription_id + purpose + expiry timestamp
Signed with HMAC-SHA256 using a secret from Secrets Manager (production)
or environment variable (local development).

Security notes:
- Tokens are time-limited (configurable expiry)
- Each token includes its purpose (verify/unsubscribe/auth) to prevent cross-use
- Secret is cached for Lambda warm starts
"""
import hashlib
import hmac
import os
import time
import base64
import json
import logging
from typing import Optional

import boto3

logger = logging.getLogger(__name__)

# --- Secret Loading (with Secrets Manager support) ---
_cached_secret: Optional[str] = None


def _load_token_secret() -> str:
    """Load token signing secret from Secrets Manager or environment.

    Priority:
    1. Cached value (Lambda warm start)
    2. AWS Secrets Manager (if TOKEN_SECRET_ARN is set)
    3. TOKEN_SECRET environment variable (local dev / testing fallback)
    4. Raise error if none available
    """
    global _cached_secret
    if _cached_secret:
        return _cached_secret

    # Try Secrets Manager first (production)
    secret_arn = os.environ.get("TOKEN_SECRET_ARN")
    if secret_arn:
        try:
            client = boto3.client("secretsmanager")
            response = client.get_secret_value(SecretId=secret_arn)
            secret_value = response["SecretString"]
            # Secret may be JSON with a "token_secret" key, or plain string
            try:
                parsed = json.loads(secret_value)
                _cached_secret = parsed.get("token_secret", secret_value)
            except json.JSONDecodeError:
                _cached_secret = secret_value
            logger.info("Token secret loaded from Secrets Manager")
            return _cached_secret
        except Exception as e:
            logger.error(f"Failed to load secret from Secrets Manager: {e}")
            # Fall through to env var

    # Fallback to environment variable (local development)
    env_secret = os.environ.get("TOKEN_SECRET")
    if env_secret and env_secret != "change-me-in-production":
        _cached_secret = env_secret
        logger.info("Token secret loaded from environment variable")
        return _cached_secret

    # Final fallback for development only
    if os.environ.get("ENVIRONMENT", "production") in ("dev", "test", "local"):
        _cached_secret = "healthsignals-dev-secret-NOT-FOR-PRODUCTION"
        logger.warning("Using development-only token secret — NOT SAFE FOR PRODUCTION")
        return _cached_secret

    raise RuntimeError(
        "No token secret configured. Set TOKEN_SECRET_ARN (Secrets Manager) "
        "or TOKEN_SECRET (env var for development)."
    )


def get_token_secret() -> str:
    """Public accessor for the token secret (cached)."""
    return _load_token_secret()


# Default token expiry durations (in seconds)
TOKEN_EXPIRY = {
    "verification": 72 * 3600,      # 72 hours for email verification
    "unsubscribe": 365 * 24 * 3600, # 1 year for unsubscribe links
    "auth": 24 * 3600,              # 24 hours for API auth tokens
}


def generate_token(
    county_fips: str,
    subscription_id: str,
    purpose: str = "verification",
    expiry_seconds: Optional[int] = None,
) -> str:
    """Generate a signed, time-limited token.

    Args:
        county_fips: The county FIPS code
        subscription_id: UUID of the subscription
        purpose: Token purpose — "verification", "unsubscribe", or "auth"
        expiry_seconds: Override default expiry duration

    Returns:
        URL-safe base64-encoded token string
    """
    secret = get_token_secret()

    if expiry_seconds is None:
        expiry_seconds = TOKEN_EXPIRY.get(purpose, TOKEN_EXPIRY["auth"])

    expiry_timestamp = int(time.time()) + expiry_seconds

    # Payload: everything the token encodes
    payload = {
        "fips": county_fips,
        "sub": subscription_id,
        "purpose": purpose,
        "exp": expiry_timestamp,
    }

    # Serialize payload
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).decode("utf-8")

    # Sign with HMAC-SHA256
    signature = hmac.new(
        secret.encode("utf-8"),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()

    # Token = payload.signature (URL-safe)
    token = f"{payload_b64}.{signature}"
    return token


def validate_token(token: str, expected_purpose: Optional[str] = None) -> dict:
    """Validate a token's signature and expiry.

    Args:
        token: The token string to validate
        expected_purpose: If set, verify the token's purpose matches

    Returns:
        Decoded payload dict if valid

    Raises:
        TokenError: If token is invalid, expired, or wrong purpose
    """
    secret = get_token_secret()

    try:
        parts = token.split(".")
        if len(parts) != 2:
            raise TokenError("Invalid token format")

        payload_b64, signature = parts

        # Decode payload
        payload_bytes = base64.urlsafe_b64decode(payload_b64.encode("utf-8"))
        payload = json.loads(payload_bytes.decode("utf-8"))

        # Verify signature
        expected_sig = hmac.new(
            secret.encode("utf-8"),
            payload_bytes,
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(signature, expected_sig):
            raise TokenError("Invalid token signature")

        # Check expiry
        if payload.get("exp", 0) < time.time():
            raise TokenError("Token has expired")

        # Check purpose
        if expected_purpose and payload.get("purpose") != expected_purpose:
            raise TokenError(
                f"Token purpose mismatch: expected '{expected_purpose}', "
                f"got '{payload.get('purpose')}'"
            )

        return payload

    except (json.JSONDecodeError, base64.binascii.Error) as e:
        raise TokenError(f"Malformed token: {e}")


def generate_unsubscribe_url(
    base_url: str, county_fips: str, subscription_id: str
) -> str:
    """Generate a complete unsubscribe URL with embedded token."""
    token = generate_token(county_fips, subscription_id, purpose="unsubscribe")
    return f"{base_url}/subscription/unsubscribe?token={token}"


def generate_verification_url(
    base_url: str, county_fips: str, subscription_id: str
) -> str:
    """Generate a verification URL for double opt-in."""
    token = generate_token(county_fips, subscription_id, purpose="verification")
    return f"{base_url}/subscription/verify?token={token}"


class TokenError(Exception):
    """Raised when token validation fails."""
    pass
