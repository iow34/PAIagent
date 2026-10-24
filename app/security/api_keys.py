import hashlib
import secrets

API_KEY_PREFIX = "pai_"


def generate_api_key() -> str:
    """Creates a random secret API key."""
    return API_KEY_PREFIX + secrets.token_urlsafe(32)


def hash_api_key(api_key: str) -> str:
    """Returns the SHA-256 hash of the API key."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()
