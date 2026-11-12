"""Accounts for the dashboard login.

The Quant Engine historically authenticated one operator against a single
pbkdf2 hash in the environment. That is kept as the break-glass credential, but
this module adds a small SQLite user table so the sign-in page can offer
"Create account" and "Forgot password?" like the other two products.

Registration and reset are gated by ``DASHBOARD_SETUP_TOKEN``: the dashboard is
a private workspace, so an open sign-up form would be a hole, not a feature.

A user has a username *and* a contact email. Either one works as the sign-in
identifier, so a forgotten username is recoverable. The email is optional in
the table because accounts created before the column existed have none.
"""

from __future__ import annotations

import os
import re
import secrets
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(os.getenv("DASHBOARD_USERS_DB", Path(__file__).parent / "data" / "dashboard_users.db"))

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS users(
             username TEXT PRIMARY KEY COLLATE NOCASE,
             salt TEXT NOT NULL,
             hash TEXT NOT NULL,
             created REAL NOT NULL
           )"""
    )
    # Databases created before the email column existed get it added in place.
    columns = [row[1] for row in connection.execute("PRAGMA table_info(users)")]
    if "email" not in columns:
        connection.execute("ALTER TABLE users ADD COLUMN email TEXT")
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_email ON users(email COLLATE NOCASE)")
    connection.commit()
    return connection


def valid_email(value: str) -> bool:
    return bool(EMAIL_PATTERN.match(value)) and len(value) <= 254


def setup_token() -> str:
    return os.getenv("DASHBOARD_SETUP_TOKEN", "")


def token_ok(supplied: str) -> bool:
    """Constant-time comparison so a wrong token cannot be guessed by timing."""
    expected = setup_token()
    if not expected:
        return False
    return secrets.compare_digest(str(supplied or ""), expected)


def create(username: str, email: str, password: str, hasher) -> None:
    salt = secrets.token_hex(16)
    with _connect() as connection:
        connection.execute(
            "INSERT INTO users(username, email, salt, hash, created) VALUES(?,?,?,?,?)",
            (username, email, salt, hasher(password, salt), time.time()),
        )


def set_password(identifier: str, password: str, hasher) -> bool:
    """Set a password by username or email. Returns False when neither matches."""
    salt = secrets.token_hex(16)
    with _connect() as connection:
        cursor = connection.execute(
            "UPDATE users SET salt=?, hash=? WHERE username=? OR email=?",
            (salt, hasher(password, salt), identifier, identifier),
        )
        return cursor.rowcount > 0


def verify(identifier: str, password: str, hasher) -> bool:
    with _connect() as connection:
        row = connection.execute(
            "SELECT salt, hash FROM users WHERE username=? OR email=?", (identifier, identifier)
        ).fetchone()
    if row is None:
        # Burn the same work as a real check so a missing user and a wrong
        # password take the same time.
        hasher(password or "x", "0" * 32)
        return False
    return secrets.compare_digest(hasher(password or "", row["salt"]), row["hash"])


def username_taken(username: str) -> bool:
    with _connect() as connection:
        return connection.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone() is not None


def email_taken(email: str) -> bool:
    with _connect() as connection:
        return connection.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone() is not None


def exists(identifier: str) -> bool:
    with _connect() as connection:
        return connection.execute(
            "SELECT 1 FROM users WHERE username=? OR email=?", (identifier, identifier)
        ).fetchone() is not None


def count() -> int:
    with _connect() as connection:
        return int(connection.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"])
