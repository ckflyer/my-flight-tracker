"""Auth groundwork for multi-user support.

Today there's realistically one pilot account, but every function here is
written to be user-scoped from day one — nothing here assumes a single
tenant. Opening up real public signup later is a matter of exposing
create_user() through an unrestricted route, not rearchitecting this.

Password hashing uses PBKDF2-SHA256 (stdlib `hashlib`, no extra dependency).
"""
from __future__ import annotations

import binascii
import hashlib
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .db import get_connection

SECRET_KEY_FILE = Path(__file__).resolve().parent.parent / "data" / "secret_key.txt"
PBKDF2_ITERATIONS = 260_000


def get_or_create_secret_key() -> str:
    """Cookie-signing key for session middleware. Generated once and
    persisted to disk — must stay stable across restarts or every logged-in
    session (pilot and viewer alike) gets invalidated on every deploy."""
    SECRET_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    if SECRET_KEY_FILE.exists():
        key = SECRET_KEY_FILE.read_text().strip()
        if key:
            return key
    key = secrets.token_hex(32)
    SECRET_KEY_FILE.write_text(key)
    return key


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"{binascii.hexlify(salt).decode()}${binascii.hexlify(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, dk_hex = stored.split("$", 1)
        salt = binascii.unhexlify(salt_hex)
        expected = binascii.unhexlify(dk_hex)
    except Exception:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return secrets.compare_digest(dk, expected)


def _generate_unique_share_code(conn) -> str:
    for _ in range(50):
        code = f"{secrets.randbelow(100_000):05d}"
        existing = conn.execute("SELECT 1 FROM users WHERE share_code = ?", (code,)).fetchone()
        if not existing:
            return code
    # Practically unreachable at this scale (100k possible codes), but never
    # loop forever.
    raise RuntimeError("could not generate a unique share code")


RECOVERY_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"  # no 0/O/1/I/L — avoids ambiguous chars


def generate_recovery_code() -> str:
    parts = ["".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(4)) for _ in range(3)]
    return "-".join(parts)


def set_recovery_code(user_id: int) -> str:
    """Generates a new recovery code, stores only its hash, and returns the
    plaintext once — callers must show it to the user immediately, since it
    can never be retrieved again after this."""
    code = generate_recovery_code()
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE users SET recovery_code_hash = ? WHERE id = ?",
            (hash_password(code), user_id),
        )
        conn.commit()
    finally:
        conn.close()
    return code


def reset_password_with_recovery_code(username: str, code: str, new_password: str) -> bool:
    """Verifies the recovery code and resets the password if it matches.
    Immediately rotates to a new recovery code on success (returned
    separately via set_recovery_code by the caller) since the old one is
    now spent — a recovery code is meant to be single-use."""
    user = get_user_by_username(username)
    if not user or not user.get("recovery_code_hash"):
        return False
    if not verify_password(code.strip(), user["recovery_code_hash"]):
        return False
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(new_password), user["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    return True


def count_users() -> int:
    conn = get_connection()
    try:
        return conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    finally:
        conn.close()


def create_user(username: str, password: str, email: str = "") -> int:
    is_first = count_users() == 0
    conn = get_connection()
    try:
        share_code = _generate_unique_share_code(conn)
        cur = conn.execute(
            """
            INSERT INTO users (username, password_hash, email, share_code, is_admin, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (username.strip(), hash_password(password), email.strip(), share_code,
             1 if is_first else 0, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        user_id = cur.lastrowid
    finally:
        conn.close()
    claim_orphaned_data(user_id)
    return user_id


def list_all_users() -> list:
    """For the admin panel in Settings — every registered account."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, username, email, share_code, is_admin, created_at FROM users ORDER BY id ASC"
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def delete_user(user_id: int) -> None:
    """Removes an account and everything it owns (schedule, legacy positions).

    flight_tracks is deliberately NOT touched. Tracks are keyed by flight
    rather than by user — they're a record of where an aircraft went, and
    another account may have the same flight on its schedule. Deleting one
    user must not erase a shared track out from under everyone else. Old
    tracks age out on their own via the retention prune in track.py.
    """
    conn = get_connection()
    try:
        conn.execute("DELETE FROM legs WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM positions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()


def claim_orphaned_data(user_id: int) -> None:
    """Attach any pre-existing legs/positions with no owner (data from
    before multi-user support existed) to this account. Only affects rows
    that truly have no owner yet, so this is safe to call unconditionally.

    "No owner" is either NULL or 0. The legs table can't store NULL owners
    since the composite (user_id, id) primary key went in, so unowned legs
    now park on sentinel 0 instead — no real account can be 0, because
    SQLite's AUTOINCREMENT starts at 1."""
    conn = get_connection()
    try:
        conn.execute("UPDATE legs SET user_id = ? WHERE user_id IS NULL OR user_id = 0", (user_id,))
        conn.execute("UPDATE positions SET user_id = ? WHERE user_id IS NULL OR user_id = 0", (user_id,))
        conn.commit()
    finally:
        conn.close()


def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def get_user_by_share_code(code: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM users WHERE share_code = ?", (code.strip(),)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def regenerate_share_code(user_id: int) -> str:
    """New code for this user — instantly revokes anyone still using the
    old one, since viewer sessions are checked against the current code on
    every request rather than being valid forever once granted."""
    conn = get_connection()
    try:
        new_code = _generate_unique_share_code(conn)
        conn.execute("UPDATE users SET share_code = ? WHERE id = ?", (new_code, user_id))
        conn.commit()
    finally:
        conn.close()
    return new_code
