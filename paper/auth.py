"""Single-user password login with signed, stateless session cookies.

The session cookie is "<expiry>.<nonce>.<hmac>". The signing key combines a persistent
secret with a hash of the password, so changing APP_PASSWORD signs everyone out.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import deque

COOKIE_NAME = "pt_session"
SESSION_SECONDS = 30 * 24 * 3600  # stay signed in on your phone for a month
ATTEMPT_WINDOW = 15 * 60
MAX_ATTEMPTS_PER_IP = 8
MAX_ATTEMPTS_TOTAL = 30


class Auth:
    def __init__(self, password: str, secret: str):
        self.enabled = bool(password)
        self._password_digest = hashlib.sha256(password.encode("utf-8")).digest()
        self._key = hashlib.sha256(secret.encode("utf-8") + self._password_digest).digest()
        self._attempts = {}
        self._all_attempts = deque()
        self._lock = threading.Lock()

    def check_password(self, candidate) -> bool:
        if not isinstance(candidate, str) or not candidate:
            return False
        digest = hashlib.sha256(candidate.encode("utf-8")).digest()
        return hmac.compare_digest(digest, self._password_digest)

    def issue(self, now: float | None = None) -> str:
        payload = f"{int((now or time.time()) + SESSION_SECONDS)}.{secrets.token_hex(8)}"
        return f"{payload}.{self._sign(payload)}"

    def verify(self, token: str | None, now: float | None = None) -> bool:
        if not token or token.count(".") != 2:
            return False
        payload, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, self._sign(payload)):
            return False
        try:
            return int(payload.split(".", 1)[0]) > (now or time.time())
        except ValueError:
            return False

    def allow_attempt(self, client: str, now: float | None = None) -> bool:
        """Throttle password guesses per client and overall."""
        now = now or time.time()
        with self._lock:
            for q in [self._all_attempts, *self._attempts.values()]:
                while q and now - q[0] > ATTEMPT_WINDOW:
                    q.popleft()
            mine = self._attempts.setdefault(client, deque())
            if len(mine) >= MAX_ATTEMPTS_PER_IP or len(self._all_attempts) >= MAX_ATTEMPTS_TOTAL:
                return False
            mine.append(now)
            self._all_attempts.append(now)
            return True

    def _sign(self, payload: str) -> str:
        return hmac.new(self._key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
