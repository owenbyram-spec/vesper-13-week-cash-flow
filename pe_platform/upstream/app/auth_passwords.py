"""Local password hashing (bcrypt) for the dev/legacy password path.

bcrypt reads only the first 72 bytes of a password, so longer passwords are refused
when set and never verified: two long passwords sharing a prefix must not match.
Hashes are standard ``$2b$`` strings, including the ones passlib wrote before.
"""
import bcrypt
from fastapi import HTTPException, status


MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_BYTES = 72  # bcrypt's input limit, counted in UTF-8 bytes


def _hashable_password(password: str) -> bytes:
    encoded = password.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Password must be at most {MAX_PASSWORD_BYTES} bytes long (UTF-8)",
        )
    if b"\x00" in encoded:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Password may not contain NUL characters")
    return encoded


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_hashable_password(password), bcrypt.gensalt()).decode("ascii")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    if not plain_password or not hashed_password:
        return False
    encoded = plain_password.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        return False
    try:
        return bcrypt.checkpw(encoded, hashed_password.encode("ascii"))
    except ValueError:
        # Not a bcrypt hash (e.g. the "stytch-managed" placeholder), or input bcrypt rejects.
        return False


def validate_password_strength(password: str) -> None:
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters long",
        )
