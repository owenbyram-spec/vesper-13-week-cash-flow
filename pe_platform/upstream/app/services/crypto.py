"""Application-layer AEAD encryption of secrets at rest (AES-256-GCM).

Every secret that lands in a DB column (QBO OAuth access/refresh tokens, per-tenant
LLM API keys, the SMTP password) is AEAD-encrypted before write and decrypted only at
point-of-use, inside a *versioned* envelope so the key can rotate without bricking
stored ciphertext. The plaintext (and the key) never reach a log.

Envelope
--------
``<prefix>base64(nonce ‖ ciphertext)`` where ``nonce`` is a fresh 12-byte (96-bit)
CSPRNG value per encryption and ``ciphertext`` is ``AESGCM(key).encrypt(nonce, pt, None)``.

Two version sentinels share the SAME AES-256-GCM primitive and the SAME wire format;
the version is purely a *label* the decrypt/sweep paths dispatch on:

  - ``enc:v1:`` — the legacy sentinel the primary write path still emits, so existing
    storage-CHECK constraints (``ck_system_settings_email_smtp_pass_encrypted``,
    ``ck_tenant_llm_credentials_keys_encrypted``) and existing tests that assert the
    ``enc:v1:`` prefix keep passing unchanged.
  - ``enc:v2:`` — the *rotation-aware* sentinel. It marks a value written by the
    key-rotation re-encrypt sweep (``scripts/ops/reencrypt_secrets.py``). Its only
    semantic difference from v1 is the label; the AES-256-GCM primitive and the
    nonce handling are IDENTICAL (per the token-encryption-at-rest skill: "v2 simply
    marks written under the rotation-aware scheme"). The DB CHECK constraints are
    amended (migration ``b7f4d2a9c1e6``) to accept ``enc:v1:`` OR ``enc:v2:`` so a
    v2 value is storable.

Key rotation (dual-key decrypt + re-encrypt sweep)
--------------------------------------------------
Rotating ``SECRET_ENCRYPTION_KEY`` WITHOUT a version-tagged, multi-key decrypt path
bricks every stored ciphertext (AES-GCM fails closed with InvalidTag on data written
under the old key). This module keeps the *previous* key(s) loadable so decrypt tries
the current key first, then each configured previous key, for BOTH v1 and v2 values.

Procedure (see also ``scripts/ops/reencrypt_secrets.py`` header):

  1. Generate a new AES key.  ``SECRET_ENCRYPTION_KEY_PREVIOUS=<the old key>``
                              ``SECRET_ENCRYPTION_KEY=<the new key>``  (deploy both).
     → the app now writes new ciphertext under the NEW key; existing ciphertext still
       DECRYPTS because the OLD key is retained as a "previous" key.
  2. Apply migration ``b7f4d2a9c1e6`` (widens the CHECK constraints to accept enc:v2:),
     then run ``python scripts/ops/reencrypt_secrets.py --apply`` — it decrypts every
     stored secret with whichever key works and re-encrypts it under the CURRENT key.
  3. Once the sweep reports 0 rows remaining under a previous key, DROP
     ``SECRET_ENCRYPTION_KEY_PREVIOUS`` and redeploy. Rotation complete.

``SECRET_ENCRYPTION_KEY_PREVIOUS`` empty/unset == today's single-key behavior exactly.

Fail-closed (unchanged)
-----------------------
A missing key still raises in production (config.py gates a MISSING key; this module
gates a PRESENT-but-non-AES key). The SHA-256 passphrase derivation is a DEV-ONLY
convenience and raises in non-development environments — for the primary key AND for
each configured previous key.
"""
import base64
import binascii
import hashlib
import logging
import os
from typing import List, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..config import settings

logger = logging.getLogger(__name__)

# Version sentinels. ENCRYPTED_PREFIX is kept as a back-compat alias (imported by
# callers/tests) and remains the version the primary write path emits.
V1_PREFIX = "enc:v1:"
V2_PREFIX = "enc:v2:"
ENCRYPTED_PREFIX = V1_PREFIX

# All prefixes decrypt() / the idempotence guard recognize as "already ciphertext".
SUPPORTED_PREFIXES = (V1_PREFIX, V2_PREFIX)

# The version encrypt_secret() emits by default. Kept at V1 so existing storage-CHECK
# constraints and tests that assert enc:v1: are unaffected; the rotation sweep writes
# V2 explicitly. Flip to V2_PREFIX (one line) once every environment has migration
# b7f4d2a9c1e6 applied and the enc:v1:-hardcoded tests are updated.
CURRENT_PREFIX = V1_PREFIX

# Environments where the SHA-256 passphrase fallback is permitted. Mirrors
# config.py:_validate_required_secrets is_dev — keep the two in sync.
_DEV_ENVIRONMENTS = {"development", "dev", "test"}


def _is_dev() -> bool:
    return settings.environment.lower() in _DEV_ENVIRONMENTS


def _material_to_key(material: str, *, label: str = "SECRET_ENCRYPTION_KEY") -> bytes:
    """Turn key MATERIAL into 32/24/16 raw AES key bytes.

    A base64-encoded 16/24/32-byte value is used verbatim. Anything else is a
    passphrase: derived via SHA-256 in DEV only, and REFUSED (fail-closed) in
    non-development environments — a present-but-non-AES key is encryption theatre.
    No key material is ever logged.
    """
    try:
        decoded = base64.urlsafe_b64decode(material + "=" * (-len(material) % 4))
    except (binascii.Error, ValueError):
        decoded = None
    if decoded is not None and len(decoded) in {16, 24, 32}:
        return decoded

    if not _is_dev():
        raise RuntimeError(
            f"{label} must be a base64-encoded 16/24/32-byte AES key in "
            "non-development environments; refusing to derive an encryption key from a "
            "passphrase (config.py gates a missing key; this gates a present non-AES key)."
        )

    logger.warning(
        "%s is not a base64-encoded AES key; deriving the key via SHA-256 "
        "(dev-only fallback — this raises in non-development environments)",
        label,
    )
    return hashlib.sha256(material.encode("utf-8")).digest()


def _key_bytes() -> bytes:
    """The CURRENT (primary) encryption key — used for every new encryption."""
    material = settings.secret_encryption_key or settings.secret_key
    if not material:
        raise RuntimeError("SECRET_ENCRYPTION_KEY or SECRET_KEY is required for secret encryption")
    return _material_to_key(material)


def _previous_keys() -> List[bytes]:
    """Retained previous key(s), most-recent-first, for decrypt during a rotation.

    Sourced from ``SECRET_ENCRYPTION_KEY_PREVIOUS`` (comma-separated). Empty/unset
    yields ``[]`` — i.e. today's single-key behavior exactly. Each entry is validated
    as an AES key just like the primary (fail-closed on a non-AES value in prod).
    """
    raw = getattr(settings, "secret_encryption_key_previous", "") or ""
    keys: List[bytes] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        keys.append(_material_to_key(item, label="SECRET_ENCRYPTION_KEY_PREVIOUS"))
    return keys


def is_encrypted(value: Optional[str]) -> bool:
    """True iff ``value`` carries a recognized encryption envelope prefix (v1 or v2)."""
    return isinstance(value, str) and value.startswith(SUPPORTED_PREFIXES)


def _decrypt_with(value: str, key: bytes) -> str:
    """Strip the version prefix and AEAD-decrypt the payload under ONE key.

    Raises InvalidTag if ``key`` did not encrypt ``value`` (fail-closed — never
    returns garbage plaintext). The nonce is the first 12 bytes of the payload.
    """
    for prefix in SUPPORTED_PREFIXES:
        if value.startswith(prefix):
            body = value[len(prefix):]
            break
    else:  # pragma: no cover - callers gate on is_encrypted() first
        raise ValueError("value does not carry a recognized encryption envelope prefix")
    payload = base64.urlsafe_b64decode(body.encode("ascii"))
    nonce, ciphertext = payload[:12], payload[12:]
    return AESGCM(key).decrypt(nonce, ciphertext, None).decode("utf-8")


def encrypt_secret(value: Optional[str], *, version: Optional[str] = None) -> Optional[str]:
    """AEAD-encrypt ``value`` under the current key inside a versioned envelope.

    Idempotent: an already-encrypted value (any supported version) is returned
    unchanged, so this is safe to call over rows a backfill may already have
    processed. ``version`` selects the sentinel to emit (defaults to CURRENT_PREFIX
    = enc:v1:); the rotation sweep passes ``version=V2_PREFIX``.
    """
    if value is None or is_encrypted(value):
        return value
    prefix = version or CURRENT_PREFIX
    if prefix not in SUPPORTED_PREFIXES:
        raise ValueError(f"unsupported encryption version prefix: {prefix!r}")
    aesgcm = AESGCM(_key_bytes())
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, value.encode("utf-8"), None)
    return prefix + base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")


def decrypt_secret(value: Optional[str]) -> Optional[str]:
    """AEAD-decrypt a versioned envelope, trying the current key then each previous key.

    A non-encrypted value (plaintext / None) passes through unchanged. During a key
    rotation, ciphertext written under a retained previous key still decrypts. If NO
    configured key decrypts the value, the AES-GCM InvalidTag propagates (fail-closed —
    never returns ciphertext or garbage). Works identically for enc:v1: and enc:v2:.
    """
    if not is_encrypted(value):
        return value
    # Current (primary) key first. _key_bytes() raises on a missing/non-AES key in
    # prod — that fail-closed behavior propagates unchanged.
    try:
        return _decrypt_with(value, _key_bytes())
    except InvalidTag as exc:
        last_error: InvalidTag = exc
    # Then each retained previous key (only parsed if the current key did not match,
    # so a good primary key still decrypts current-key data even if a previous-key
    # env value is malformed).
    for key in _previous_keys():
        try:
            return _decrypt_with(value, key)
        except InvalidTag:
            continue
    raise last_error


def is_encrypted_with_current_key(value: Optional[str]) -> bool:
    """True iff ``value`` is ciphertext that the CURRENT (primary) key decrypts.

    Used by the re-encrypt sweep to skip rows already under the current key without a
    write. False for plaintext/None and for ciphertext only a previous key can decrypt.
    """
    if not is_encrypted(value):
        return False
    try:
        _decrypt_with(value, _key_bytes())
        return True
    except InvalidTag:
        return False
