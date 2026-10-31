"""Login/session tests."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paper.auth import MAX_ATTEMPTS_PER_IP, Auth  # noqa: E402


class AuthTest(unittest.TestCase):
    def setUp(self):
        self.auth = Auth("correct horse battery", "secret")

    def test_password_check(self):
        self.assertTrue(self.auth.check_password("correct horse battery"))
        self.assertFalse(self.auth.check_password("wrong"))
        self.assertFalse(self.auth.check_password(None))

    def test_token_roundtrip_expiry_and_tamper(self):
        token = self.auth.issue(now=1000)
        self.assertTrue(self.auth.verify(token, now=2000))
        self.assertFalse(self.auth.verify(token, now=1000 + 31 * 86400))
        exp, nonce, sig = token.split(".")
        self.assertFalse(self.auth.verify(f"{int(exp) + 99}.{nonce}.{sig}", now=2000))
        self.assertFalse(self.auth.verify("garbage", now=2000))

    def test_password_change_invalidates_sessions(self):
        token = self.auth.issue()
        self.assertFalse(Auth("a different password", "secret").verify(token))

    def test_attempt_throttle(self):
        for _ in range(MAX_ATTEMPTS_PER_IP):
            self.assertTrue(self.auth.allow_attempt("1.2.3.4", now=100))
        self.assertFalse(self.auth.allow_attempt("1.2.3.4", now=101))
        self.assertTrue(self.auth.allow_attempt("5.6.7.8", now=101))
        self.assertTrue(self.auth.allow_attempt("1.2.3.4", now=100 + 16 * 60))


if __name__ == "__main__":
    unittest.main()
