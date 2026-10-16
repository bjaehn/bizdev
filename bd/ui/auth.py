"""Password login for the hosted UI: signed, expiring session cookies and a login-attempt throttle.

Design notes (single shared password, one user):
  * the password never leaves the environment (BD_UI_PASSWORD) and must be at least 16 characters, or the hosted
    server refuses to start - there is no default and no way to run it open;
  * the session cookie is `s1.<expiry>.<HMAC-SHA256(secret, "s1." + expiry)>`: nothing secret inside, tamper-proof,
    expires after `session_hours`; changing the password invalidates every session;
  * failed logins are throttled per client and globally, with exponential back-off, so guessing is impractical."""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

MIN_PASSWORD_LENGTH = 16


class AuthConfigError(Exception):
    pass


@dataclass
class Hosted:
    password: str
    session_hours: int = 12
    secret: bytes = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if len(self.password) < MIN_PASSWORD_LENGTH:
            raise AuthConfigError(
                f"BD_UI_PASSWORD must be at least {MIN_PASSWORD_LENGTH} characters. The hosted UI will not run without a "
                "strong password (use a password manager or `python -c \"import secrets;print(secrets.token_urlsafe(24))\"`).")
        # Derived, so a changed password invalidates all sessions; never the password itself.
        self.secret = hashlib.sha256(b"bd-ui-session|" + self.password.encode()).digest()

    @property
    def csrf(self) -> str:
        return hmac.new(self.secret, b"csrf", hashlib.sha256).hexdigest()

    def check_password(self, attempt: str) -> bool:
        a = hashlib.sha256(attempt.encode()).digest()
        b = hashlib.sha256(self.password.encode()).digest()
        return hmac.compare_digest(a, b)          # equal-length digests: no length leak

    def make_session(self, now: float | None = None) -> str:
        expiry = int((time.time() if now is None else now) + self.session_hours * 3600)
        return f"s1.{expiry}.{self._sig(expiry)}"

    def session_ok(self, value: str, now: float | None = None) -> bool:
        parts = (value or "").split(".")
        if len(parts) != 3 or parts[0] != "s1" or not parts[1].isdigit():
            return False
        expiry = int(parts[1])
        if expiry < (time.time() if now is None else now):
            return False
        return hmac.compare_digest(parts[2], self._sig(expiry))

    def _sig(self, expiry: int) -> str:
        return hmac.new(self.secret, f"s1.{expiry}".encode(), hashlib.sha256).hexdigest()


class Throttle:
    """Per-client and global limit on failed logins. After `free` failures a client is locked out for
    base * 2^(n - free) seconds (capped); a global ceiling stops distributed guessing."""

    def __init__(self, free: int = 5, base: float = 30.0, cap: float = 900.0, global_per_hour: int = 60,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.free, self.base, self.cap, self.global_per_hour, self.clock = free, base, cap, global_per_hour, clock
        self._lock = threading.Lock()
        self._fails: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}
        self._global: list[float] = []

    def retry_after(self, client: str) -> int:
        """Seconds until this client may try again (0 = may try now)."""
        with self._lock:
            now = self.clock()
            self._global = [t for t in self._global if now - t < 3600]
            if len(self._global) >= self.global_per_hour:
                return int(3600 - (now - self._global[0])) + 1
            return max(0, int(self._locked_until.get(client, 0) - now) + (1 if self._locked_until.get(client, 0) > now else 0))

    def failed(self, client: str) -> None:
        with self._lock:
            now = self.clock()
            fails = [t for t in self._fails.get(client, []) if now - t < 3600] + [now]
            self._fails[client] = fails
            self._global.append(now)
            if len(fails) >= self.free:
                self._locked_until[client] = now + min(self.cap, self.base * 2 ** (len(fails) - self.free))
            if len(self._fails) > 5000:                       # bound memory under a flood of distinct clients
                self._fails.clear()
                self._locked_until.clear()

    def succeeded(self, client: str) -> None:
        with self._lock:
            self._fails.pop(client, None)
            self._locked_until.pop(client, None)
