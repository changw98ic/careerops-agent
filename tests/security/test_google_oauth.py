"""Tests for real AES-256-GCM encryption in google_oauth module."""

from __future__ import annotations

import os

import pytest
from cryptography.exceptions import InvalidTag

from careerops.integrations.google_oauth import (
    EncryptedToken,
    _aes_gcm_decrypt,
    _aes_gcm_encrypt,
    decrypt_token,
    encrypt_token,
)


@pytest.fixture()
def envelope_key() -> bytes:
    """A random 256-bit key for testing."""
    return os.urandom(32)


@pytest.fixture()
def other_key() -> bytes:
    """A different random 256-bit key for wrong-key tests."""
    return os.urandom(32)


class TestAesGcmRoundtrip:
    """Low-level _aes_gcm_encrypt / _aes_gcm_decrypt roundtrip."""

    def test_encrypt_decrypt_roundtrip(self, envelope_key: bytes) -> None:
        plaintext = b"super-secret-oauth-token-value"
        nonce = os.urandom(12)

        ciphertext = _aes_gcm_encrypt(envelope_key, nonce, plaintext)
        recovered = _aes_gcm_decrypt(envelope_key, nonce, ciphertext)

        assert recovered == plaintext
        # Ciphertext must differ from plaintext (real encryption, not XOR passthrough)
        assert ciphertext != plaintext

    def test_ciphertext_includes_auth_tag(self, envelope_key: bytes) -> None:
        plaintext = b"short"
        nonce = os.urandom(12)

        ciphertext = _aes_gcm_encrypt(envelope_key, nonce, plaintext)

        # AESGCM.encrypt appends 16-byte tag: len(ciphertext) == len(plaintext) + 16
        assert len(ciphertext) == len(plaintext) + 16

    def test_different_nonces_produce_different_ciphertexts(self, envelope_key: bytes) -> None:
        plaintext = b"same-plaintext"
        nonce_a = os.urandom(12)
        nonce_b = os.urandom(12)

        ct_a = _aes_gcm_encrypt(envelope_key, nonce_a, plaintext)
        ct_b = _aes_gcm_encrypt(envelope_key, nonce_b, plaintext)

        assert ct_a != ct_b

    def test_empty_plaintext_roundtrips(self, envelope_key: bytes) -> None:
        nonce = os.urandom(12)
        ciphertext = _aes_gcm_encrypt(envelope_key, nonce, b"")
        recovered = _aes_gcm_decrypt(envelope_key, nonce, ciphertext)
        assert recovered == b""

    def test_large_plaintext_roundtrips(self, envelope_key: bytes) -> None:
        plaintext = os.urandom(10_000)
        nonce = os.urandom(12)
        ciphertext = _aes_gcm_encrypt(envelope_key, nonce, plaintext)
        recovered = _aes_gcm_decrypt(envelope_key, nonce, ciphertext)
        assert recovered == plaintext


class TestAesGcmTamperDetection:
    """AES-GCM must reject tampered ciphertext."""

    def test_tampered_ciphertext_raises(self, envelope_key: bytes) -> None:
        plaintext = b"integrity-protected-data"
        nonce = os.urandom(12)

        ciphertext = _aes_gcm_encrypt(envelope_key, nonce, plaintext)

        # Flip a bit in the ciphertext body
        tampered = bytearray(ciphertext)
        tampered[0] ^= 0xFF
        tampered = bytes(tampered)

        with pytest.raises(InvalidTag):
            _aes_gcm_decrypt(envelope_key, nonce, tampered)

    def test_truncated_ciphertext_raises(self, envelope_key: bytes) -> None:
        plaintext = b"data"
        nonce = os.urandom(12)
        ciphertext = _aes_gcm_encrypt(envelope_key, nonce, plaintext)

        with pytest.raises(InvalidTag):
            _aes_gcm_decrypt(envelope_key, nonce, ciphertext[:-1])

    def test_wrong_key_raises(self, envelope_key: bytes, other_key: bytes) -> None:
        plaintext = b"key-mismatch-test"
        nonce = os.urandom(12)

        ciphertext = _aes_gcm_encrypt(envelope_key, nonce, plaintext)

        with pytest.raises(InvalidTag):
            _aes_gcm_decrypt(other_key, nonce, ciphertext)

    def test_wrong_nonce_raises(self, envelope_key: bytes) -> None:
        plaintext = b"nonce-mismatch-test"
        nonce = os.urandom(12)
        wrong_nonce = os.urandom(12)

        ciphertext = _aes_gcm_encrypt(envelope_key, nonce, plaintext)

        with pytest.raises(InvalidTag):
            _aes_gcm_decrypt(envelope_key, wrong_nonce, ciphertext)


class TestHighLevelEncryptDecryptToken:
    """encrypt_token / decrypt_token public API roundtrip."""

    def test_roundtrip_via_public_api(self, envelope_key: bytes) -> None:
        plaintext = "ya29.a0ARrdaM-this-is-a-google-token"

        encrypted = encrypt_token(plaintext, envelope_key=envelope_key, key_id="k-2026-07")
        recovered = decrypt_token(encrypted, envelope_key=envelope_key)

        assert recovered == plaintext

    def test_encrypted_token_fields_populated(self, envelope_key: bytes) -> None:
        encrypted = encrypt_token("tok", envelope_key=envelope_key, key_id="k1")

        assert isinstance(encrypted, EncryptedToken)
        assert len(encrypted.nonce) == 12
        assert len(encrypted.ciphertext) > 0
        assert encrypted.key_id == "k1"
        assert encrypted.created_at is not None

    def test_repr_masks_ciphertext(self, envelope_key: bytes) -> None:
        encrypted = encrypt_token("secret-value", envelope_key=envelope_key, key_id="k1")
        r = repr(encrypted)

        assert "secret-value" not in r
        assert "k1" in r

    def test_wrong_key_at_public_api_level_raises(
        self, envelope_key: bytes, other_key: bytes
    ) -> None:
        encrypted = encrypt_token("tok", envelope_key=envelope_key, key_id="k1")

        with pytest.raises(InvalidTag):
            decrypt_token(encrypted, envelope_key=other_key)

    def test_key_validation_enforced(self) -> None:
        with pytest.raises(ValueError, match="32 bytes"):
            _aes_gcm_encrypt(b"short", os.urandom(12), b"data")

        with pytest.raises(ValueError, match="12 bytes"):
            _aes_gcm_encrypt(os.urandom(32), b"short", b"data")
