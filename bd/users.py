"""People who can sign in to the hosted app: their own password, a one-time sign-up link, and sessions that end when a password changes or a person is removed.

Everyone who signs in sees and can do everything; what a person has is a name in the change log. The shared password (BD_UI_PASSWORD) is not a row here: it is the
owner's sign-in, kept so the app can always be reached and people can always be added.

  * a password is stored only as a salted scrypt hash, never in the log and never shown;
  * a sign-up link carries a random token that is stored only as a SHA-256 hash, works once, and expires after INVITE_DAYS; an admin sees the link once, when it is made;
  * a session cookie holds the person's id, an expiry and a signature over a per-person key that changes whenever the password does, so setting a password,
    making a new link or removing a person ends their sessions at once.
No network and no model here."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from bd.accounts import log_audit

MIN_PASSWORD = 12
INVITE_DAYS = 7
OWNER = "Owner"                                              # the shared password's name in the change log; nobody can sign up with it
_SCRYPT = (2 ** 14, 8, 1)
_LOGIN = re.compile(r"[A-Za-z0-9._@+\-]{3,80}")


@dataclass(frozen=True)
class Person:
    id: int                                                  # 0 = the owner (shared password)
    name: str


OWNER_PERSON = Person(0, OWNER)


def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now(timezone.utc)


# ---- passwords ------------------------------------------------------------------------------------------------------------------------------------
def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    n, r, p = _SCRYPT
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time check. A missing or unreadable hash is a failed check that still costs one hash, so timing does not say who exists."""
    try:
        scheme, n, r, p, salt, digest = (stored or "").split("$")
        if scheme != "scrypt":
            raise ValueError
        want = base64.b64decode(digest)
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=len(want))
        return hmac.compare_digest(got, want)
    except (ValueError, TypeError):
        hashlib.scrypt(password.encode(), salt=b"\0" * 16, n=_SCRYPT[0], r=_SCRYPT[1], p=_SCRYPT[2], dklen=32)
        return False


def password_problem(password: str, login: str = "", name: str = "") -> str | None:
    if len(password) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters (a few words together work well)."
    if len(set(password)) < 5:
        return "Use a password with more variety than that."
    if password.lower() in {login.lower(), name.lower()}:
        return "The password cannot be the same as your sign-in name."
    return None


# ---- people ---------------------------------------------------------------------------------------------------------------------------------------
def clean_name(name: str) -> str:
    return " ".join((name or "").split())[:80]


def login_problem(login: str) -> str | None:
    if not _LOGIN.fullmatch(login or ""):
        return "A sign-in name is 3 to 80 letters, digits or . _ @ + - (an email address works)."
    if login.lower() == OWNER.lower():
        return f"\"{OWNER}\" is the shared password's name; choose another."
    return None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def add_user(conn: sqlite3.Connection, name: str, login: str, actor: str, now: datetime | None = None) -> tuple[int | None, str, str | None]:
    """(user id, sign-up token, problem). The token is returned once and kept only as a hash. Caller owns the transaction."""
    name, login = clean_name(name), (login or "").strip()
    if not name:
        return None, "", "Enter the person's name."
    bad = login_problem(login)
    if bad:
        return None, "", bad
    if conn.execute("SELECT 1 FROM users WHERE login = ?", (login,)).fetchone():
        return None, "", "That sign-in name is already used."
    when, token = _now(now), _new_token()
    cur = conn.execute("INSERT INTO users(login, name, status, session_key, invite_hash, invite_expires, created, created_by) VALUES (?,?,?,?,?,?,?,?)",
                       (login, name, "invited", secrets.token_hex(16), _token_hash(token), (when + timedelta(days=INVITE_DAYS)).isoformat(), when.isoformat(), actor))
    log_audit(conn, actor, "create", None, "user", None, f"{name} ({login})")
    return cur.lastrowid, token, None


def new_link(conn: sqlite3.Connection, user_id: int, actor: str, now: datetime | None = None) -> str | None:
    """A fresh one-time link for a person who has not signed up yet or forgot their password. It ends their current password and sessions: the old
    password stops working the moment the link is made. None for someone who does not exist or was removed."""
    row = conn.execute("SELECT name, login, status FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None or row["status"] == "removed":
        return None
    when, token = _now(now), _new_token()
    conn.execute("UPDATE users SET status = 'invited', pw_hash = NULL, session_key = ?, invite_hash = ?, invite_expires = ? WHERE id = ?",
                 (secrets.token_hex(16), _token_hash(token), (when + timedelta(days=INVITE_DAYS)).isoformat(), user_id))
    log_audit(conn, actor, "update", None, "user_link", row["login"], f"new sign-up link for {row['name']}")
    return token


def remove_user(conn: sqlite3.Connection, user_id: int, actor: str) -> bool:
    row = conn.execute("SELECT name, login, status FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None or row["status"] == "removed":
        return False
    conn.execute("UPDATE users SET status = 'removed', pw_hash = NULL, invite_hash = NULL, invite_expires = NULL, session_key = ? WHERE id = ?",
                 (secrets.token_hex(16), user_id))
    log_audit(conn, actor, "delete", None, "user", f"{row['name']} ({row['login']})", None)
    return True


def invited_user(conn: sqlite3.Connection, token: str, now: datetime | None = None) -> sqlite3.Row | None:
    """The person a sign-up link belongs to, when it is the right token, not used, not expired."""
    if not token:
        return None
    row = conn.execute("SELECT * FROM users WHERE invite_hash = ? AND status = 'invited'", (_token_hash(token),)).fetchone()
    if row is None or not row["invite_expires"] or datetime.fromisoformat(row["invite_expires"]) < _now(now):
        return None
    return row


def accept_invite(conn: sqlite3.Connection, token: str, password: str, now: datetime | None = None) -> tuple[sqlite3.Row | None, str | None]:
    """Set the person's own password with a sign-up link (once). Returns (the person, a problem to show). Caller owns the transaction."""
    row = invited_user(conn, token, now)
    if row is None:
        return None, "This link has already been used, has expired, or is not right. Ask for a new one."
    bad = password_problem(password, row["login"], row["name"])
    if bad:
        return None, bad
    when = _now(now).isoformat()
    cur = conn.execute("UPDATE users SET pw_hash = ?, status = 'active', invite_hash = NULL, invite_expires = NULL, session_key = ?, last_login = ? "
                       "WHERE id = ? AND invite_hash = ?", (hash_password(password), secrets.token_hex(16), when, row["id"], _token_hash(token)))
    if cur.rowcount != 1:                                                       # another request used the link first
        return None, "This link has already been used, has expired, or is not right. Ask for a new one."
    log_audit(conn, row["name"], "update", None, "user_password", row["login"], "set their own password")
    return conn.execute("SELECT * FROM users WHERE id = ?", (row["id"],)).fetchone(), None


def authenticate(conn: sqlite3.Connection, login: str, password: str, now: datetime | None = None) -> sqlite3.Row | None:
    """The person for this sign-in name and password, or None. Costs one hash either way."""
    row = conn.execute("SELECT * FROM users WHERE login = ? AND status = 'active'", ((login or "").strip(),)).fetchone()
    if not verify_password(password, row["pw_hash"] if row else None) or row is None:
        return None
    conn.execute("UPDATE users SET last_login = ? WHERE id = ?", (_now(now).isoformat(), row["id"]))
    return row


def active_user(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE id = ? AND status = 'active'", (user_id,)).fetchone()


def list_users(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM users WHERE status != 'removed' ORDER BY lower(name)").fetchall()
