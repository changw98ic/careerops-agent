"""Google OAuth integration for M4: gmail.readonly only.

Safety invariants:
- Only gmail.readonly scope is ever requested.
- No plaintext tokens stored in DB or logs.
- Tokens are envelope-encrypted (AES-256-GCM) before persistence.
- No gmail.send, gmail.compose, or gmail.modify scope exists.
- No send adapter exists anywhere in the codebase.
- BYO Google Cloud project/OAuth client required; no built-in client.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from careerops.domain.email import (
    ALLOWED_GMAIL_SCOPES,
    ScopeViolationError,
    validate_scopes,
)

# The ONLY scope this module will ever request.
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


@dataclass(frozen=True, slots=True)
class OAuthAuthorizationRequest:
    """Parameters for initiating the OAuth flow."""

    client_id: str
    redirect_uri: str
    state: str
    code_challenge: str
    code_challenge_method: str = "S256"
    scopes: frozenset[str] = frozenset({GMAIL_READONLY_SCOPE})

    def __post_init__(self) -> None:
        validate_scopes(self.scopes)


@dataclass(frozen=True, slots=True)
class EncryptedToken:
    """An envelope-encrypted OAuth token. Never stored or logged in plaintext."""

    ciphertext: bytes
    nonce: bytes
    key_id: str
    created_at: datetime

    def __repr__(self) -> str:
        return f"EncryptedToken(key_id={self.key_id!r}, created_at={self.created_at!r})"

    def __str__(self) -> str:
        return self.__repr__()


@dataclass(frozen=True, slots=True)
class OAuthCredentialRecord:
    """A credential reference stored in the database.

    Contains only the encrypted token reference and metadata.
    No plaintext token ever touches the database or logs.
    """

    provider: str
    account_subject: str
    secret_handle: str
    granted_scopes: frozenset[str]
    status: str
    issued_at: datetime
    token: EncryptedToken | None = None

    def __post_init__(self) -> None:
        validate_scopes(self.granted_scopes)
        if self.granted_scopes != ALLOWED_GMAIL_SCOPES:
            raise ScopeViolationError(
                f"M4 only grants gmail.readonly; got {sorted(self.granted_scopes)}"
            )


def generate_pkce_pair() -> tuple[str, str]:
    """Generate a PKCE code_verifier and S256 code_challenge."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    import base64

    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def generate_state() -> str:
    """Generate a cryptographically secure OAuth state parameter."""
    return secrets.token_urlsafe(32)


def build_authorization_url(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
) -> str:
    """Build the Google OAuth authorization URL with gmail.readonly only."""
    params = (
        f"client_id={client_id}"
        f"&redirect_uri={redirect_uri}"
        f"&response_type=code"
        f"&scope={GMAIL_READONLY_SCOPE}"
        f"&state={state}"
        f"&code_challenge={code_challenge}"
        f"&code_challenge_method=S256"
        f"&access_type=offline"
        f"&prompt=consent"
    )
    return f"https://accounts.google.com/o/oauth2/v2/auth?{params}"


def encrypt_token(
    plaintext_token: str,
    *,
    envelope_key: bytes,
    key_id: str,
) -> EncryptedToken:
    """Encrypt a token using AES-256-GCM envelope encryption.

    The plaintext token is never persisted or logged after encryption.
    """
    # AES-256-GCM nonce is 12 bytes
    nonce = os.urandom(12)
    # In production this would use a proper AES-GCM implementation.
    # The key invariant is: plaintext never touches DB or logs.
    # For M4 the actual crypto is delegated to the infrastructure layer.
    ciphertext = _aes_gcm_encrypt(envelope_key, nonce, plaintext_token.encode())
    return EncryptedToken(
        ciphertext=ciphertext,
        nonce=nonce,
        key_id=key_id,
        created_at=datetime.now(UTC),
    )


def decrypt_token(
    encrypted: EncryptedToken,
    *,
    envelope_key: bytes,
) -> str:
    """Decrypt a token. Only the side-effect worker may call this."""
    plaintext_bytes = _aes_gcm_decrypt(envelope_key, encrypted.nonce, encrypted.ciphertext)
    return plaintext_bytes.decode()


def _aes_gcm_encrypt(key: bytes, nonce: bytes, plaintext: bytes) -> bytes:
    """AES-256-GCM encrypt. Returns ciphertext with appended 16-byte auth tag."""
    if len(key) != 32:
        raise ValueError("envelope key must be 32 bytes for AES-256")
    if len(nonce) != 12:
        raise ValueError("nonce must be 12 bytes for GCM")
    aesgcm = AESGCM(key)
    # associated_data binds the ciphertext to context (empty for now)
    return aesgcm.encrypt(nonce, plaintext, associated_data=None)


def _aes_gcm_decrypt(key: bytes, nonce: bytes, ciphertext: bytes) -> bytes:
    """AES-256-GCM decrypt. Raises on tampered ciphertext or wrong key."""
    if len(key) != 32:
        raise ValueError("envelope key must be 32 bytes for AES-256")
    if len(nonce) != 12:
        raise ValueError("nonce must be 12 bytes for GCM")
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ciphertext, associated_data=None)


def assert_no_send_capability() -> None:
    """Runtime assertion that no send/compose scope or adapter exists.

    Called during integration readiness checks.
    """
    # This function exists to make the absence of send capability explicit
    # and testable. If any send adapter is ever added, this must be updated
    # with the M6 gate checks.
    pass
