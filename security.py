"""Deployment validation and the outer HTTP security boundary."""

import base64
import binascii
import hmac
import os
from pathlib import Path
from urllib.parse import urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse

ROOT = Path(__file__).resolve().parent
PROBES = {"/health/live", "/health/ready"}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; "
       "connect-src 'self' https://generativelanguage.googleapis.com wss://generativelanguage.googleapis.com; "
       "object-src 'none'; frame-ancestors 'none'; base-uri 'none'")


def is_operator_path(path):
    return path.startswith("/ops") or path == "/api/ops" or path.startswith("/api/ops/")


def validate_configuration(assets=ROOT / "dist"):
    # ASVS 13.1: explicit environment and documented, fail-closed shared configuration.
    mode = os.getenv("APP_ENV", "development")
    if mode not in ("development", "staging", "production"):
        raise RuntimeError("APP_ENV must be development, staging or production.")
    if mode == "production":
        raise RuntimeError("Live production is blocked: identity, airline, payment and handoff integrations are simulated.")
    origin = os.getenv("APP_ORIGIN", "")
    if origin:
        try:
            parts = urlsplit(origin)
            valid = (parts.scheme in ("http", "https") and parts.hostname and not parts.username
                     and not parts.password and not parts.path and not parts.query and not parts.fragment)
            parts.port
        except ValueError:
            valid = False
        if not valid:
            raise RuntimeError("APP_ORIGIN must be a canonical HTTP(S) origin without credentials or a path.")
    if mode == "development":
        return
    if not origin.startswith("https://"):
        raise RuntimeError("Shared deployments require an HTTPS APP_ORIGIN.")
    for name in ("ACCESS_KEY", "OPS_ACCESS_KEY"):
        if len(os.getenv(name, "")) < 32:
            raise RuntimeError(f"{name} must contain at least 32 characters from a cryptographic secret generator.")
    if hmac.compare_digest(os.environ["ACCESS_KEY"].encode(), os.environ["OPS_ACCESS_KEY"].encode()):
        raise RuntimeError("Customer and operator credentials must differ.")
    if not os.getenv("GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY") == "your_key_here":
        raise RuntimeError("Configure the voice provider credential before shared deployment.")
    if os.getenv("CONSENT_MODEL", "gemini-3.6-flash") == os.getenv("CONSENT_VERIFIER_MODEL", "gemini-3.5-flash"):
        raise RuntimeError("Approval judges must use different models.")
    if not all((assets / file).is_file() for file in ("index.html", "app.js", "ops.html")):
        raise RuntimeError("Build the frontend assets before shared deployment.")
    database_url = os.getenv("TURSO_DATABASE_URL", "")
    if database_url.startswith(("https:", "libsql:")):
        if not os.getenv("TURSO_AUTH_TOKEN"):
            raise RuntimeError("A remote database requires its authentication token.")
    elif not database_url or not Path(database_url.removeprefix("file:")).is_absolute():
        raise RuntimeError("Shared deployments require a remote database or an absolute path on persistent storage.")


class SecurityBoundary:
    """Wrap the entire ASGI app so denials and server errors also receive headers."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        path = scope["path"]

        async def secure_send(message):
            if message["type"] == "http.response.start":
                out = MutableHeaders(scope=message)
                # ASVS 3.4: these controls also apply to errors, redirects and static files.
                out["Cache-Control"] = "no-store"
                out["X-Content-Type-Options"] = "nosniff"
                out["Referrer-Policy"] = "no-referrer"
                out["Permissions-Policy"] = "microphone=(self), camera=()"
                out["Content-Security-Policy"] = CSP
                out["Cross-Origin-Opener-Policy"] = "same-origin"
                out["Cross-Origin-Resource-Policy"] = "same-origin"
                if scope.get("scheme") == "https":
                    out["Strict-Transport-Security"] = "max-age=31536000"
            await send(message)

        denial = None
        origin = os.getenv("APP_ORIGIN", "")
        shared = os.getenv("APP_ENV", "development") != "development"
        if path not in PROBES:
            allowed = {urlsplit(origin).netloc.lower()} if origin else {"localhost", "127.0.0.1", "[::1]", "testserver"}
            authority = headers.get("host", "").lower()
            host = authority.rsplit(":", 1)[0] if authority.count(":") == 1 else authority
            valid_host = authority in allowed if origin else host in allowed
            if len(headers.getlist("host")) != 1 or not valid_host:
                denial = JSONResponse({"error": "Invalid host."}, status_code=400)
            elif shared and (not origin.startswith("https://") or scope.get("scheme") != "https"):
                # Only the ASGI server's configured trusted proxy may set the scheme.
                denial = JSONResponse({"error": "HTTPS is required."}, status_code=400)
            elif is_operator_path(path):
                key = os.getenv("OPS_ACCESS_KEY", "")
                if len(key) < 32:
                    denial = JSONResponse({"error": "Operator access is not configured."}, status_code=503)
                else:
                    supplied = headers.get("authorization", "")
                    valid = False
                    if supplied.startswith("Basic ") and len(supplied) <= 1024:
                        try:
                            user, separator, password = base64.b64decode(supplied[6:], validate=True).partition(b":")
                            valid = bool(separator) and hmac.compare_digest(user, b"operator") and hmac.compare_digest(password, key.encode())
                        except (ValueError, binascii.Error):
                            pass
                    if not valid:
                        # ASVS 8.2.1: customer cookies never authorise operational access.
                        denial = JSONResponse({"error": "Operator authentication required."}, status_code=401,
                                              headers={"WWW-Authenticate": 'Basic realm="operations", charset="UTF-8"'})
        if denial is not None:
            return await denial(scope, receive, secure_send)
        await self.app(scope, receive, secure_send)
