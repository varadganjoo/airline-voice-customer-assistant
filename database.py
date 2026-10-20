"""SQLite/libSQL storage. Call state, booking and receipt share one transaction."""

from contextlib import contextmanager, nullcontext
from hashlib import sha256
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from db_transport import DatabaseFailure, HttpDatabase

from tools import ROUTES, generate_booking, now_ms
import telemetry

PASSENGER = "Michael Jackson"


class RateLimitExceeded(Exception):
    def __init__(self, retry_after):
        super().__init__("Too many requests. Please wait and try again.")
        self.retry_after = retry_after


def digest(text):
    return sha256(text.encode()).hexdigest()


def name_key(name):
    return re.sub(r"[^a-z]", "", name.lower())


def empty_state():
    return {"notes": "", "reservation": None, "proposals": [], "actions": [], "version": 0}


class Store:
    def __init__(self, url=None, auth_token=None):
        url = url or os.getenv("TURSO_DATABASE_URL") or "file:demo.sqlite"
        auth_token = auth_token if auth_token is not None else os.getenv("TURSO_AUTH_TOKEN", "")
        if url.startswith("file:"):
            parts = urlsplit(url)
            if parts.netloc:
                raise ValueError("Use a local file path or file:/// URL for the database.")
            url = unquote(parts.path)
            if re.match(r"^/[A-Za-z]:/", url):
                url = url[1:]
        self.deadline = time.monotonic() + 20
        self.tx_deadline = None
        # One connection per request; graph nodes run sequentially on worker threads.
        self.db = HttpDatabase(url, auth_token) if url.startswith(("libsql:", "https:")) else sqlite3.connect(url, isolation_level=None, timeout=3, check_same_thread=False)
        self.execute("PRAGMA foreign_keys=ON")

    def execute(self, sql, *args):
        self.check_deadline()
        if isinstance(self.db, HttpDatabase):
            self.db.deadline = min(self.deadline, self.tx_deadline or self.deadline)
        try:
            return self.db.execute(sql, args)
        except sqlite3.Error as error:
            raise DatabaseFailure(isinstance(error, sqlite3.OperationalError)) from None

    def check_deadline(self):
        if time.monotonic() >= min(self.deadline, self.tx_deadline or self.deadline):
            raise TimeoutError("Booking operation timed out. Check its receipt before retrying.")

    def rows(self, sql, *args):
        cursor = self.execute(sql, *args)
        names = [col[0] for col in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    @contextmanager
    def transaction(self):
        with telemetry.span("db.transaction", kind=telemetry.SpanKind.CLIENT, **{"db.system": "libsql" if isinstance(self.db, HttpDatabase) else "sqlite"}) as current:
            try:
                with self._transaction():
                    yield self
            except (ValueError, RateLimitExceeded):  # Expected refusals: rolled back, not a fault.
                current.set_attribute("outcome", "rolled_back")
                raise
            except BaseException as error:
                telemetry.mark_error(current, error)
                raise

    @contextmanager
    def _transaction(self):
        self.tx_deadline = min(self.deadline, time.monotonic() + 4)
        try:
            self.execute("BEGIN IMMEDIATE")
            yield self
            self.check_deadline()
            if isinstance(self.db, HttpDatabase):
                self.db.deadline = self.tx_deadline
            self.db.commit()
        except BaseException:
            try:
                self.db.rollback()
            except Exception:
                pass  # Preserve the original error; expired remote transactions roll back.
            raise
        finally:
            self.tx_deadline = None

    def init(self):
        with self.transaction():
            for sql in [
                "CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY, name TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id), expires INTEGER NOT NULL)",
                "CREATE TABLE IF NOT EXISTS bookings(reference TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id), version INTEGER NOT NULL DEFAULT 0, scenario TEXT NOT NULL, data TEXT NOT NULL)",
                "CREATE INDEX IF NOT EXISTS bookings_owner ON bookings(account_id)",
                "CREATE TABLE IF NOT EXISTS actions(id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id), reference TEXT NOT NULL REFERENCES bookings(reference), receipt TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS calls(id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id), data TEXT NOT NULL, expires INTEGER NOT NULL)",
                "CREATE TABLE IF NOT EXISTS verification(session_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL, reference TEXT NOT NULL, code_hash TEXT NOT NULL, expires INTEGER NOT NULL, attempts INTEGER NOT NULL, verified_until INTEGER NOT NULL)",
                "CREATE TABLE IF NOT EXISTS inbox(id TEXT PRIMARY KEY, session_hash TEXT NOT NULL, payload TEXT NOT NULL, expires INTEGER NOT NULL)",
                "CREATE TABLE IF NOT EXISTS limits(key TEXT PRIMARY KEY, count INTEGER NOT NULL, expires INTEGER NOT NULL)",
                "CREATE TABLE IF NOT EXISTS limit_events(key TEXT NOT NULL, at INTEGER NOT NULL, expires INTEGER NOT NULL)",
                "CREATE INDEX IF NOT EXISTS limit_events_window ON limit_events(key, at)",
                "CREATE INDEX IF NOT EXISTS limit_events_expiry ON limit_events(expires)",
                "CREATE TABLE IF NOT EXISTS tool_receipts(call_id TEXT NOT NULL REFERENCES calls(id), request_id TEXT NOT NULL, account_id TEXT NOT NULL REFERENCES accounts(id), fingerprint TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(call_id,request_id))",
            ]:
                self.execute(sql)

    def create_caller(self):
        account = {"id": str(uuid4()), "name": PASSENGER}
        routes = secrets.SystemRandom().sample(ROUTES, 6)
        with self.transaction():
            self.execute("INSERT INTO accounts VALUES(?,?)", account["id"], account["name"])
            for i, route in enumerate(routes):
                booking = generate_booking(account["name"], route=route, disrupted=i == 5, refundable=i % 2 == 0)
                self.execute("INSERT INTO bookings(reference,account_id,scenario,data) VALUES(?,?,?,?)", booking["reference"], account["id"], "demo", json.dumps(booking))
        return account

    def create_session(self, account):
        token = secrets.token_hex(32)
        with self.transaction():
            self.execute("DELETE FROM sessions WHERE expires < ?", now_ms())
            self.execute("INSERT INTO sessions VALUES(?,?,?)", digest(token), account["id"], now_ms() + 24 * 60 * 60_000)
        return token

    def authenticate(self, token):
        if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{64}", token):
            return None
        rows = self.rows("SELECT a.id,a.name FROM accounts a JOIN sessions s ON a.id=s.account_id WHERE s.token_hash=? AND s.expires>?", digest(token), now_ms())
        return rows[0] if rows else None

    def list(self, account_id):
        return [dict(json.loads(r["data"]), scenario=r["scenario"]) for r in self.rows("SELECT scenario,data FROM bookings WHERE account_id=? ORDER BY reference", account_id)]

    def lookup(self, account_id, reference, name=None):
        rows = self.rows("SELECT * FROM bookings WHERE account_id=? AND reference=?", account_id, re.sub(r"[^A-Z0-9]", "", reference.upper()))
        if not rows or (name and name_key(json.loads(rows[0]["data"])["passenger"]) != name_key(name)):
            raise ValueError("No reservation matches that confirmation number and name.")
        row = rows[0]
        actions = self.rows("SELECT receipt FROM actions WHERE account_id=? AND reference=? ORDER BY rowid DESC LIMIT 50", account_id, row["reference"])
        return {"version": row["version"], "notes": "", "proposals": [], "actions": [json.loads(a["receipt"]) for a in reversed(actions)], "reservation": json.loads(row["data"])}

    def request_verification(self, account_id, session_hash, reference, name):
        booking = self.lookup(account_id, reference, name)["reservation"]
        code = str(100000 + secrets.randbelow(900000))
        expires = now_ms() + 5 * 60_000
        payload = {"id": str(uuid4()), "kind": "identity", "reference": booking["reference"], "message": "Skyway Airlines: your verification code is below. Skyway will only ask for it when you call us.", "to": booking["mobile"], "code": code}
        self.execute("INSERT INTO inbox VALUES(?,?,?,?)", str(uuid4()), session_hash, json.dumps(payload), expires)
        self.execute("INSERT INTO verification VALUES(?,?,?,?,?,0,0) ON CONFLICT(session_hash) DO UPDATE SET reference=excluded.reference,code_hash=excluded.code_hash,expires=excluded.expires,attempts=0,verified_until=0", session_hash, account_id, booking["reference"], digest(code), expires)
        return {"sent": True, "sentTo": "mobile number " + booking["mobile"], "instruction": "Tell the caller you just texted a six-digit code to that number and ask them to read it to you. Never guess it."}

    def verify(self, account_id, session_hash, spoken):
        # Spoken codes arrive as "1 4 7 1 7 3" or "147-173"; only six digits in total form a valid attempt.
        digits = re.sub(r"[\s-]", "", spoken)
        if not re.fullmatch(r"[0-9\s-]+", spoken) or not re.fullmatch(r"[0-9]{6}", digits):
            return {"error": "Please provide exactly six digits, one at a time. No attempt was used.", "invalidFormat": True}
        spoken = digits
        rows = self.rows("SELECT * FROM verification WHERE session_hash=? AND account_id=?", session_hash, account_id)
        row = rows[0] if rows else None
        if not row or row["expires"] < now_ms() or row["attempts"] >= 5 or row["verified_until"] > 0:
            return {"error": "Verification code expired, used, or unavailable. Offer to resend the code.", "needsNewCode": True}
        self.execute("UPDATE verification SET attempts=attempts+1 WHERE session_hash=? AND account_id=?", session_hash, account_id)
        if not hmac.compare_digest(row["code_hash"], digest(spoken)):
            remaining = 4 - row["attempts"]
            return {"error": "That code does not match. Ask for all six digits one at a time, or resend on request.", "attemptsRemaining": remaining, "needsNewCode": remaining == 0}
        self.execute("UPDATE verification SET verified_until=? WHERE session_hash=? AND account_id=?", now_ms() + 12 * 60 * 60_000, session_hash, account_id)
        return {"verified": True, "reference": row["reference"]}

    def verified_reference(self, account_id, session_hash):
        rows = self.rows("SELECT reference FROM verification WHERE session_hash=? AND account_id=? AND verified_until>?", session_hash, account_id, now_ms())
        if not rows:
            raise ValueError("Verify the caller with a one-time code before accessing booking details or making changes.")
        return rows[0]["reference"]

    def inbox(self, session_hash):
        self.execute("DELETE FROM inbox WHERE expires < ?", now_ms())
        return [json.loads(r["payload"]) for r in self.rows("SELECT payload FROM inbox WHERE session_hash=? ORDER BY rowid DESC LIMIT 8", session_hash)]

    def commit(self, account_id, reference, version, reservation, receipt):
        if not self.db.in_transaction:
            raise RuntimeError("Booking commit requires an active transaction.")
        prior = self.rows("SELECT receipt FROM actions WHERE id=? AND account_id=? AND reference=?", receipt["id"], account_id, reference)
        if prior:
            return json.loads(prior[0]["receipt"])
        changed = self.execute("UPDATE bookings SET data=?,version=version+1 WHERE account_id=? AND reference=? AND version=?", json.dumps(reservation), account_id, reference, version)
        if changed.rowcount != 1:
            raise ValueError("Booking changed or is unavailable. Look it up again before making a new proposal.")
        self.execute("INSERT INTO actions VALUES(?,?,?,?)", receipt["id"], account_id, reference, json.dumps(receipt))
        return receipt

    def new_call(self, account_id):
        id = str(uuid4())
        data = {"state": empty_state(), "results": {}, "cancelled": [], "ended": False, "workflow": {"stage": "identify", "turn": None}}
        data["state"]["interactionNotes"] = [{"id": id, "timestamp": now_ms(), "kind": "activity", "text": "Call session opened."}]
        with self.transaction():
            self.execute("DELETE FROM tool_receipts WHERE call_id IN (SELECT id FROM calls WHERE expires < ?)", now_ms())
            self.execute("DELETE FROM calls WHERE expires < ?", now_ms())
            self.execute("INSERT INTO calls VALUES(?,?,?,?)", id, account_id, json.dumps(data), now_ms() + 24 * 60 * 60_000)
        return id

    @contextmanager
    def call(self, account_id, id):
        if not isinstance(id, str) or not id or len(id) > 80:
            raise ValueError("Invalid call.")
        with self.transaction():
            rows = self.rows("SELECT data FROM calls WHERE id=? AND account_id=? AND expires>?", id, account_id, now_ms())
            if not rows:
                raise ValueError("Call unavailable. Start a new call.")
            data = json.loads(rows[0]["data"])
            if data["ended"]:
                raise ValueError("Call has ended.")
            if "workflow" not in data:
                data["workflow"] = {"stage": "verified" if data["state"].get("reservation") else "identify", "turn": None}
                # Pending proposals from the JS build have no fresh-turn evidence.
                for p in data["state"]["proposals"]:
                    if p["status"] == "pending":
                        p["status"] = "superseded"
            yield data
            self.execute("UPDATE calls SET data=?,expires=? WHERE id=? AND account_id=?", json.dumps(data), now_ms() + 24 * 60 * 60_000, id, account_id)

    def limit(self, key, max_count, window_ms):
        if window_ms <= 0 or max_count < 0:
            raise ValueError("Invalid rate limit configuration.")
        now = now_ms()
        with (nullcontext() if self.db.in_transaction else self.transaction()):
            self.execute("DELETE FROM limit_events WHERE expires <= ?", now)
            usage = self.rows("SELECT COUNT(*) AS count, MIN(at) AS oldest FROM limit_events WHERE key=? AND at>?", key, now - window_ms)[0]
            if usage["count"] >= max_count:
                wait_ms = usage["oldest"] + window_ms - now if usage["oldest"] is not None else window_ms
                raise RateLimitExceeded(max(1, (wait_ms + 999) // 1000))
            self.execute("INSERT INTO limit_events(key,at,expires) VALUES(?,?,?)", key, now, now + window_ms)

    def tool_receipt(self, account_id, call_id, request_id):
        rows = self.rows("SELECT fingerprint,result FROM tool_receipts WHERE account_id=? AND call_id=? AND request_id=?", account_id, call_id, request_id)
        return {"fingerprint": rows[0]["fingerprint"], "result": json.loads(rows[0]["result"])} if rows else None

    def save_tool_receipt(self, account_id, call_id, request_id, fingerprint, result):
        self.execute("INSERT INTO tool_receipts VALUES(?,?,?,?,?)", call_id, request_id, account_id, fingerprint, json.dumps(result))

    def close(self):
        self.db.close()
