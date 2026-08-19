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
    """Cookie-signing key for session middleware. If this value changes,
    every session cookie in existence stops verifying and everyone — pilot
    and viewers alike — is silently logged out.

    It used to live only in data/secret_key.txt. That file is gitignored,
    so `git reset --hard` in update.sh leaves it alone in theory; in
    practice a key sitting in a loose file next to a directory that gets
    rebuilt, re-mounted and reset on every deploy has too many ways to go
    missing, and losing it is exactly the "signed out again" symptom.

    So the key now lives in the database, alongside the accounts it
    authenticates. If the schedule survives an update, the login does too —
    one thing to persist instead of two. Order of preference:

      1. PT_SECRET_KEY in the environment, if set. Lets the key be pinned
         in docker-compose.yml and makes it recoverable by hand.
      2. The app_meta row in the database.
      3. The old text file, adopted on first run after this change so
         nobody is logged out by the upgrade itself.
      4. Failing all of those, a fresh one — first run on a new install.
    """
    env_key = os.environ.get("PT_SECRET_KEY", "").strip()
    if env_key:
        return env_key

    conn = get_connection()
    try:
        # Created here rather than relying on init_db having run first,
        # because the session middleware is wired up at import time.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS app_meta ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        row = conn.execute(
            "SELECT value FROM app_meta WHERE key = 'secret_key'"
        ).fetchone()
        if row and (row["value"] or "").strip():
            return row["value"].strip()

        key = ""
        try:
            if SECRET_KEY_FILE.exists():
                key = SECRET_KEY_FILE.read_text().strip()
        except OSError:
            key = ""
        adopted = bool(key)
        if not key:
            key = secrets.token_hex(32)

        conn.execute(
            "INSERT OR REPLACE INTO app_meta (key, value) VALUES ('secret_key', ?)",
            (key,),
        )
        conn.commit()
        print("[auth] session key " +
              ("adopted from data/secret_key.txt" if adopted else "generated") +
              " and stored in the database")
    finally:
        conn.close()

    # Still written to the file as a second copy, so the key can be read off
    # disk if the database is ever rebuilt from scratch.
    try:
        SECRET_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        SECRET_KEY_FILE.write_text(key)
    except OSError:
        pass
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
    """A code no live invite anywhere is already using.

    Checked against BOTH tables, and across all pilots, not merely unique
    per pilot: two households handed the same five digits would each see
    the other's position feed. `users.share_code` is still checked because
    it still carries the UNIQUE index, so a collision there would fail the
    insert rather than be caught here.

    Revoked codes are deliberately still in the way. Reissuing five digits
    that a removed viewer has sitting in a text message is the one failure
    this whole feature exists to prevent.
    """
    for _ in range(50):
        code = f"{secrets.randbelow(100_000):05d}"
        taken = conn.execute(
            "SELECT 1 FROM users WHERE share_code = ? "
            "UNION ALL SELECT 1 FROM share_codes WHERE code = ?",
            (code, code)).fetchone()
        if not taken:
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
             1 if is_first else 0, datetime.utcnow().isoformat() + "Z"),
        )
        user_id = cur.lastrowid
        # AND THE INVITE ROW (1.23.0). Since auth resolves codes through
        # `share_codes`, a pilot whose code exists only on `users` has a
        # code that does not work — and the db.py backfill would not
        # rescue them until the next restart. A brand-new account handing
        # out five digits that log nobody in is the worst possible first
        # impression, and it is silent: the pilot sees a code on their
        # page and the family sees "invalid code".
        #
        # Written in the SAME transaction as the user, so an account can
        # never exist without its first invite.
        conn.execute(
            "INSERT OR IGNORE INTO share_codes "
            "(user_id, code, name, created_at) VALUES (?,?,?,?)",
            (user_id, share_code, "Family",
             datetime.now(timezone.utc).isoformat()))
        conn.commit()
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


def count_admins() -> int:
    conn = get_connection()
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM users WHERE is_admin = 1").fetchone()
        return int(row["n"]) if row else 0
    finally:
        conn.close()


def set_admin(user_id: int, make_admin: bool) -> bool:
    """Grant or remove admin. Returns whether anything changed. (1.6.0)

    Before this there was no way to create a second admin at all —
    `create_user` sets the flag on whoever registers first and nothing else
    ever touched it. On a self-hosted box that meant losing the first
    account lost administration of the install permanently.

    THE LAST-ADMIN GUARD is the only interesting part. Removing the final
    admin leaves a database with real flight data in it that nobody can
    administer, and there is no recovery path from the app — it would mean
    opening SQLite by hand on the NAS. So the last one cannot be removed.
    Refused silently rather than raising: this is reached from a form post,
    and the caller re-renders a page that will simply still show that
    person as an admin, which is the truth.
    """
    conn = get_connection()
    try:
        row = conn.execute("SELECT is_admin FROM users WHERE id = ?",
                           (user_id,)).fetchone()
        if row is None:
            return False
        currently = bool(row["is_admin"])
        if currently == bool(make_admin):
            return False
        if currently and not make_admin:
            others = conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE is_admin = 1 AND id != ?",
                (user_id,)).fetchone()
            if not others or int(others["n"]) == 0:
                print("[auth] refused to remove the last admin")
                return False
        conn.execute("UPDATE users SET is_admin = ? WHERE id = ?",
                     (1 if make_admin else 0, user_id))
        conn.commit()
        return True
    finally:
        conn.close()


def delete_user(user_id: int) -> None:
    """Removes an account and everything it owns.

    `positions` is deliberately NOT touched. Tracks are keyed by flight
    rather than by user — they're a record of where an aircraft went, and
    another account may have the same flight on its schedule. Deleting one
    user must not erase a shared track out from under everyone else.
    Orphaned tracks are swept by the 30-day purge in flights.py.
    """
    conn = get_connection()
    try:
        # Only their ROSTER goes. Flights are shared with other crew:
        # deleting one account must not erase a colleague's flight or the
        # tracking data everyone on it depends on. Flights nobody has left
        # are swept by purge_old().
        conn.execute("DELETE FROM roster WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()


def claim_orphaned_data(user_id: int) -> None:
    """Attach any flights with no owner (data carried over from before
    multi-user support existed) to this account.

    "No owner" is sentinel 0 — no real account can be 0, because SQLite's
    AUTOINCREMENT starts at 1. Only affects rows that truly have no owner,
    so this is safe to call unconditionally.
    """
    conn = get_connection()
    try:
        conn.execute("UPDATE roster SET user_id = ? WHERE user_id = 0", (user_id,))
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
    """Resolve a code to its pilot, via `share_codes` (1.23.0).

    A REVOKED row does not resolve, which is the entire point: that is how
    one viewer is cut off without touching anyone else's access.

    Every code that existed before 1.23.0 was copied into this table by
    the backfill in db.py, so no family had to be re-sent anything.
    """
    code = code.strip()
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT u.* FROM share_codes s JOIN users u ON u.id = s.user_id "
            "WHERE s.code = ? AND s.revoked = 0", (code,)).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE share_codes SET last_seen_at = ? WHERE code = ?",
                     (_now_iso(), code))
        conn.commit()
    finally:
        conn.close()
    return dict(row)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def share_codes_for(user_id: int) -> list:
    """Every invite this pilot has, newest last. Revoked ones included —
    the page shows them struck through rather than vanishing them, so
    "I removed that" is distinguishable from "that was never there"."""
    conn = get_connection()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM share_codes WHERE user_id = ? "
            "ORDER BY revoked ASC, id ASC", (user_id,))]
    finally:
        conn.close()


def add_share_code(user_id: int, name: str) -> str:
    """A new invite alongside the existing ones. Adding never disturbs a
    code already in somebody's hands."""
    conn = get_connection()
    try:
        code = _generate_unique_share_code(conn)
        conn.execute(
            "INSERT INTO share_codes (user_id, code, name, created_at) "
            "VALUES (?,?,?,?)",
            (user_id, code, (name or "").strip()[:40] or "Share", _now_iso()))
        conn.commit()
    finally:
        conn.close()
    return code


def rename_share_code(user_id: int, code_id: int, name: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE share_codes SET name = ? WHERE id = ? AND user_id = ?",
            ((name or "").strip()[:40] or "Share", code_id, user_id))
        conn.commit()
    finally:
        conn.close()


def set_share_code_revoked(user_id: int, code_id: int, revoked: bool) -> None:
    """Revoke or restore ONE invite.

    Scoped by user_id in the WHERE clause, not merely by the id from the
    form: without it any pilot could revoke any other pilot's invite by
    posting a number.
    """
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE share_codes SET revoked = ? WHERE id = ? AND user_id = ?",
            (1 if revoked else 0, code_id, user_id))
        conn.commit()
    finally:
        conn.close()


def regenerate_one_share_code(user_id: int, code_id: int) -> Optional[str]:
    """New digits for one invite, keeping its name and its place.

    The old digits stop working immediately — same effect as the old
    whole-account regenerate, but aimed at one person instead of everyone.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT 1 FROM share_codes WHERE id = ? AND user_id = ?",
            (code_id, user_id)).fetchone()
        if row is None:
            return None
        code = _generate_unique_share_code(conn)
        conn.execute("UPDATE share_codes SET code = ?, last_seen_at = NULL "
                     "WHERE id = ? AND user_id = ?", (code, code_id, user_id))
        conn.commit()
    finally:
        conn.close()
    return code


def regenerate_share_code(user_id: int) -> str:
    """New code for this user — instantly revokes anyone still using the
    old one, since viewer sessions are checked against the current code on
    every request rather than being valid forever once granted."""
    conn = get_connection()
    try:
        old_row = conn.execute("SELECT share_code FROM users WHERE id = ?",
                               (user_id,)).fetchone()
        new_code = _generate_unique_share_code(conn)
        conn.execute("UPDATE users SET share_code = ? WHERE id = ?", (new_code, user_id))
        # AND THE MATCHING INVITE (1.23.0). The button that calls this is
        # gone from the page, but the ROUTE is still reachable — a phone
        # with the old /flights open posts to it — and auth resolves codes
        # through `share_codes` now. Updating only `users` would leave the
        # pilot's original invite pointing at digits nothing accepts,
        # silently, while the page showed a code that worked.
        if old_row and old_row["share_code"]:
            conn.execute(
                "UPDATE share_codes SET code = ?, last_seen_at = NULL "
                "WHERE user_id = ? AND code = ?",
                (new_code, user_id, old_row["share_code"]))
        conn.commit()
    finally:
        conn.close()
    return new_code
