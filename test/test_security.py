import base64
import os
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import patch

from starlette.responses import JSONResponse


def request(app, path="/", *, host="testserver", scheme="http", headers=()):
    """Exercise the real ASGI boundary without opening a network socket."""
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
             "method": "GET", "path": path, "raw_path": path.encode(), "query_string": b"",
             "scheme": scheme, "server": (host, 443 if scheme == "https" else 80),
             "client": ("127.0.0.1", 1234), "headers": [(b"host", host.encode()), *headers]}
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    task = app(scope, receive, send)
    try:
        task.send(None)
    except StopIteration:
        pass
    else:
        task.close()
        raise AssertionError("Boundary unexpectedly waited for external I/O")
    return messages[0]["status"], dict(messages[0]["headers"])


async def downstream(scope, receive, send):
    await JSONResponse({"ok": True})(scope, receive, send)


class SecurityTests(TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"APP_ENV": "development", "APP_ORIGIN": "",
                                          "ACCESS_KEY": "", "OPS_ACCESS_KEY": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_customer_access_never_grants_operator_access(self):
        from security import SecurityBoundary
        for path in ("/ops", "/ops.html", "/ops.js", "/api/ops/summary", "/api/ops/call/123"):
            with self.subTest(path=path), patch.dict(os.environ, {"OPS_ACCESS_KEY": "o" * 48}):
                status, headers = request(SecurityBoundary(downstream), path,
                                          headers=[(b"cookie", b"access=customer-session")])
                self.assertEqual(status, 401)
                self.assertIn(b"Basic", headers[b"www-authenticate"])
                self.assertEqual(headers[b"cache-control"], b"no-store")

    def test_operator_credentials_are_separate_and_rotation_is_immediate(self):
        from security import SecurityBoundary
        encoded = base64.b64encode(b"operator:" + b"o" * 48)
        headers = [(b"authorization", b"Basic " + encoded)]
        app = SecurityBoundary(downstream)
        with patch.dict(os.environ, {"OPS_ACCESS_KEY": "o" * 48}):
            self.assertEqual(request(app, "/ops", headers=headers)[0], 200)
        with patch.dict(os.environ, {"OPS_ACCESS_KEY": "n" * 48}):
            self.assertEqual(request(app, "/ops", headers=headers)[0], 401)
        for value in (b"Basic invalid", b"Bearer ignored", b"Basic " + b"a" * 5000):
            self.assertEqual(request(app, "/ops", headers=[(b"authorization", value)])[0], 503)

    def test_every_response_has_security_headers_including_denials(self):
        from security import SecurityBoundary
        status, headers = request(SecurityBoundary(downstream), host="attacker.invalid")
        self.assertEqual(status, 400)
        self.assertEqual(headers[b"x-content-type-options"], b"nosniff")
        self.assertEqual(headers[b"referrer-policy"], b"no-referrer")
        self.assertIn(b"frame-ancestors 'none'", headers[b"content-security-policy"])

    def test_shared_host_and_https_are_enforced_without_trusting_spoofed_headers(self):
        from security import SecurityBoundary
        app = SecurityBoundary(downstream)
        with patch.dict(os.environ, {"APP_ENV": "staging", "APP_ORIGIN": "https://airline.example"}):
            self.assertEqual(request(app, host="attacker.invalid", scheme="https")[0], 400)
            self.assertEqual(request(app, host="airline.example", headers=[(b"x-forwarded-proto", b"https")])[0], 400)
            status, headers = request(app, host="airline.example", scheme="https")
            self.assertEqual(status, 200)
            self.assertEqual(headers[b"strict-transport-security"], b"max-age=31536000")
            self.assertEqual(request(app, "/health/live", host="127.0.0.1")[0], 200)

    def test_configuration_rejects_unsafe_shared_deployments_and_live_production(self):
        from security import validate_configuration
        with tempfile.TemporaryDirectory() as temp:
            assets = Path(temp)
            for file in ("index.html", "app.js", "ops.html"):
                (assets / file).write_text("built", encoding="utf-8")
            valid = {"APP_ENV": "staging", "APP_ORIGIN": "https://airline.example", "ACCESS_KEY": "c" * 48,
                     "OPS_ACCESS_KEY": "o" * 48, "GEMINI_API_KEY": "fixture-provider-key",
                     "TURSO_DATABASE_URL": str(assets / "test.sqlite"),
                     "CONSENT_MODEL": "judge-a", "CONSENT_VERIFIER_MODEL": "judge-b"}
            with patch.dict(os.environ, valid):
                validate_configuration(assets)
                for invalid in ({"ACCESS_KEY": "short"}, {"OPS_ACCESS_KEY": "c" * 48},
                                {"APP_ORIGIN": "https://user:secret@airline.example"},
                                {"APP_ORIGIN": "https://airline.example/path"}, {"GEMINI_API_KEY": ""},
                                {"TURSO_DATABASE_URL": ""}, {"TURSO_DATABASE_URL": "file:ephemeral.sqlite"},
                                {"CONSENT_VERIFIER_MODEL": "judge-a"}, {"APP_ENV": "typo"},
                                {"APP_ENV": "production"}):
                    with self.subTest(invalid=list(invalid)), patch.dict(os.environ, invalid):
                        with self.assertRaises(RuntimeError):
                            validate_configuration(assets)

    def test_probe_readiness_checks_configuration_and_policy_freshness(self):
        from types import SimpleNamespace
        from app import readiness
        store = SimpleNamespace(rows=lambda sql: [{"1": 1}])
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            self.assertEqual(readiness(store).status_code, 503)
        with patch.dict(os.environ, {"GEMINI_API_KEY": "fixture-provider-key"}), \
             patch("app.lookup_policy", return_value={"status": "unavailable"}):
            self.assertEqual(readiness(store).status_code, 503)
        with patch.dict(os.environ, {"GEMINI_API_KEY": "fixture-provider-key"}), \
             patch("app.lookup_policy", return_value={"status": "snapshot"}):
            self.assertEqual(readiness(store)["status"], "ready")

