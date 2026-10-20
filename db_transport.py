"""Small libSQL HTTP adapter: bounded requests and explicit transaction batons.

Protocol: https://docs.turso.tech/sdk/http/reference. No statement-level retries:
a lost response can mean a write succeeded; retry the complete idempotent tool.
"""

from threading import Lock
from types import SimpleNamespace
import time
from urllib.parse import urlsplit

import httpx


class DatabaseFailure(RuntimeError):
    def __init__(self, retryable=True):
        super().__init__("The booking service could not complete the request. Please try again.")
        self.retryable = retryable


_clients, _clients_lock = {}, Lock()


def shared_client(token):
    """One pooled client per credential, so each request reuses TLS connections instead of opening new ones."""
    with _clients_lock:
        client = _clients.get(token)
        if client is None or client.is_closed:
            client = _clients[token] = httpx.Client(headers={"Authorization": "Bearer " + token}, follow_redirects=False,
                                                    limits=httpx.Limits(max_connections=64, max_keepalive_connections=16))
        return client


class HttpDatabase:
    def __init__(self, url, token, client=None):
        parts = urlsplit(url.replace("libsql://", "https://", 1))
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.query or parts.fragment:
            raise ValueError("Use an HTTPS or libsql database URL.")
        self.origin = f"https://{parts.netloc}"
        self.url = self.origin + "/v2/pipeline"
        # A supplied client belongs to this connection; the shared pool outlives it.
        self.owns_client = client is not None
        self.client = client or shared_client(token)
        self.baton = None
        self.in_transaction = False
        self.deadline = time.monotonic() + 20

    def execute(self, sql, args=()):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Booking operation timed out. Check its receipt before retrying.")
        encoded = [{"type": "null"} if a is None else {"type": "integer", "value": str(a)} if isinstance(a, int) else {"type": "text", "value": a} for a in args]
        try:
            response = self.client.post(self.url, json={"baton": self.baton, "requests": [{"type": "execute", "stmt": {"sql": sql, "args": encoded, "want_rows": True}}]}, timeout=min(5, remaining))
            response.raise_for_status()
            data = response.json()
            self.baton = data["baton"]
            base = data.get("base_url")
            if base:
                # Credentials must not follow arbitrary redirects or returned hosts.
                target = urlsplit(base)
                if target.scheme != "https" or target.netloc != urlsplit(self.origin).netloc or target.username or target.query or target.fragment:
                    raise DatabaseFailure(False)
                self.url = base.rstrip("/") + "/v2/pipeline"
            result = data["results"][0]
            if result["type"] != "ok":
                raise DatabaseFailure(result.get("error", {}).get("code", "").startswith(("SQLITE_BUSY", "SQLITE_LOCKED", "TXN_TIMEOUT")))
            result = result["response"]["result"]
            def value(cell):
                return None if cell["type"] == "null" else int(cell["value"]) if cell["type"] == "integer" else float(cell["value"]) if cell["type"] == "float" else cell["value"]
            rows = [tuple(value(cell) for cell in row) for row in result["rows"]]
            if sql.startswith("BEGIN"):
                self.in_transaction = True
            elif sql in ("COMMIT", "ROLLBACK"):
                self.in_transaction = False
            return SimpleNamespace(description=[(c["name"],) for c in result["cols"]], rowcount=result["affected_row_count"], fetchall=lambda: rows)
        except httpx.TimeoutException:
            raise TimeoutError("Booking operation timed out. Check its receipt before retrying.") from None
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            raise DatabaseFailure() from None

    def commit(self):
        self.execute("COMMIT")

    def rollback(self):
        if self.in_transaction:
            self.deadline = time.monotonic() + 1
            self.execute("ROLLBACK")

    def close(self):
        try:
            if self.baton:
                self.client.post(self.url, json={"baton": self.baton, "requests": [{"type": "close"}]}, timeout=1)
        except httpx.HTTPError:
            pass  # The server expires abandoned connections/transactions.
        finally:
            if self.owns_client:
                self.client.close()
