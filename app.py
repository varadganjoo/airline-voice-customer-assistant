"""FastAPI endpoints; voice streams directly from browser to Gemini."""

import asyncio
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import hmac
import json
import logging
import os
from pathlib import Path
import time
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from google import genai
from pydantic import BaseModel, ConfigDict, Field

from database import PASSENGER, RateLimitExceeded, Store, digest
from db_transport import DatabaseFailure
from tools import lookup_policy, now_ms, trip_label
from voice import live_config, validate_tool
from workflow import process_event
from resilience import ServiceBusy, api_slots, recovery_slots, voice_provider
from uuid import uuid4
import ops
import telemetry

CALL_IDLE_LIMIT_MS = 10 * 60_000  # Server backstop; the page itself checks in after 60 s and hangs up 40 s later.
logger = logging.getLogger("airline.workflow")
logger.setLevel(logging.INFO)
logger.addHandler(logging.StreamHandler())
logger.propagate = False
app = FastAPI(title="Skyway Voice Demo", docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def admission(request: Request, call_next):
    if not request.url.path.startswith("/api/"):
        return await call_next(request)
    slots = recovery_slots if request.url.path in ("/api/tool-status", "/api/cancel", "/api/end") else api_slots
    if not slots.acquire(blocking=False):
        return JSONResponse({"error": "The service is busy. Please try again shortly.", "retryable": False}, status_code=503, headers={"Retry-After": "5"})
    request_id, started = uuid4().hex, time.monotonic()
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        logger.info(json.dumps({"event": "http_request", "request": request_id, "status": response.status_code, "elapsed_ms": int((time.monotonic() - started) * 1000)}))
        return response
    finally:
        slots.release()


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    key: str | None = Field(default=None, max_length=1024)


class TokenBody(Body):
    callId: str | None = Field(default=None, min_length=1, max_length=80)
    handle: str | None = Field(default=None, max_length=4096)


class ToolBody(Body):
    callId: str = Field(min_length=1, max_length=80)
    requestId: str = Field(min_length=1, max_length=128)
    name: str = Field(max_length=80)
    args: dict
    turnId: str | None = Field(default=None, min_length=1, max_length=80)
    # The caller turn being acted on, re-sent for judgment. Accepted only if it matches the saved turn hash.
    turnText: str | None = Field(default=None, max_length=4000)
    agentText: str | None = Field(default=None, max_length=4000)
    # Accepted for old clients, never used as evidence of approval.
    callerUtterance: str | None = Field(default=None, max_length=4000)


class TurnBody(Body):
    callId: str = Field(min_length=1, max_length=80)
    turnId: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=4000)
    proposalId: str | None = Field(default=None, min_length=1, max_length=80)
    # What the agent said since the caller's previous turn: the quote or readback the caller is answering.
    agentText: str | None = Field(default=None, max_length=4000)
    # "keypad" is the deterministic last step of the approval ladder (press 1).
    channel: Literal["voice", "keypad"] = "voice"
    # The key pressed on a keypad turn: 1 confirms, 7 cancels.
    keypadKey: Literal["1", "7"] | None = None


class CallBody(Body):
    callId: str = Field(min_length=1, max_length=80)


class CancelBody(CallBody):
    ids: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(max_length=20)


class ReceiptBody(CallBody):
    requestId: str = Field(min_length=1, max_length=128)


@app.middleware("http")
async def request_boundary(request: Request, call_next):
    if request.url.path.startswith("/api/") and request.method == "POST":
        expected = os.getenv("APP_ORIGIN") or str(request.base_url).rstrip("/")
        if request.headers.get("origin") != expected or request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            return JSONResponse({"error": "Same-origin JSON request required."}, status_code=403)
        try:
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 16000:
                    return JSONResponse({"error": "Request too large."}, status_code=413)
            # Starlette passes the cached body to downstream request validation.
            request._body = bytes(raw)
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            return JSONResponse({"error": "Invalid JSON request."}, status_code=400)
    else:
        body = {}
    access_key = os.getenv("ACCESS_KEY")
    if os.getenv("APP_ORIGIN", "").startswith("https://") and not access_key:
        return JSONResponse({"error": "Demo access is not configured."}, status_code=503)
    link_key = request.query_params.get("k") if request.method == "GET" and request.url.path == "/" else None
    body_key = body.get("key") if request.url.path == "/api/start" else None
    if access_key:
        expected = digest(access_key)
        has_cookie = hmac.compare_digest(request.cookies.get("access", ""), expected)
        valid_link = isinstance(link_key, str) and hmac.compare_digest(digest(link_key), expected)
        valid_body = isinstance(body_key, str) and hmac.compare_digest(digest(body_key), expected)
        if not (has_cookie or valid_link or valid_body):
            return JSONResponse({"error": "Open this demo using your access link."}, status_code=403, headers={"Cache-Control": "no-store"})
        if link_key is not None:
            response = RedirectResponse("/", status_code=303, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
            response.set_cookie("access", expected, max_age=30 * 86400, httponly=True, samesite="strict", secure=request.url.scheme == "https" or os.getenv("APP_ORIGIN", "").startswith("https://"))
            return response
    try:
        async with asyncio.timeout(22):
            response = await call_next(request)
    except TimeoutError:
        response = JSONResponse({"error": "Request timed out. Check the tool receipt before retrying.", "retryable": True, "outcomeUnknown": True}, status_code=504)
    except Exception:
        # Do not log provider errors, credentials, caller text, booking data or SQL.
        logger.error("request_failed path=%s", request.url.path)
        response = JSONResponse({"error": "The request could not be completed. Please try again later."}, status_code=500)
    if access_key and valid_body:
        response.set_cookie("access", expected, max_age=30 * 86400, httponly=True, samesite="strict", secure=request.url.scheme == "https" or os.getenv("APP_ORIGIN", "").startswith("https://"))
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "microphone=(self), camera=()"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self' https://generativelanguage.googleapis.com wss://generativelanguage.googleapis.com; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
    return response


UNTRACED = ("/api/health", "/api/telemetry")


@app.middleware("http")
async def observe(request: Request, call_next):
    """Outermost: one server span per API request, continuing the browser's trace when it sends traceparent."""
    path = request.url.path
    if not path.startswith("/api/") or path in UNTRACED or path.startswith("/api/ops"):
        return await call_next(request)
    buffer = telemetry.begin()
    try:
        with telemetry.span(f"{request.method} {path}", kind=telemetry.SpanKind.SERVER, context=telemetry.extract(request.headers),
                            **{"http.method": request.method, "http.route": path}) as current:
            try:
                response = await call_next(request)
            except Exception as error:
                telemetry.mark_error(current, error)
                raise
            current.set_attribute("http.status_code", response.status_code)
            if response.status_code >= 500:
                current.set_status(telemetry.Status(telemetry.StatusCode.ERROR, str(response.status_code)))
            response.headers["traceresponse"] = f"00-{format(current.get_span_context().trace_id, '032x')}-{format(current.get_span_context().span_id, '016x')}-01"
            return response
    finally:
        telemetry.flush(buffer)


@app.exception_handler(ValueError)
async def invalid_operation(request, error):
    return JSONResponse({"error": str(error)}, status_code=400)


@app.exception_handler(RateLimitExceeded)
async def rate_limited(request, error):
    return JSONResponse({"error": str(error), "retryable": False}, status_code=429,
                        headers={"Retry-After": str(error.retry_after), "Cache-Control": "no-store"})


@app.exception_handler(ServiceBusy)
async def service_busy(request, error):
    return JSONResponse({"error": str(error), "retryable": False}, status_code=503,
                        headers={"Retry-After": str(error.retry_after), "Cache-Control": "no-store"})


@app.exception_handler(DatabaseFailure)
async def database_error(request, error):
    return JSONResponse({"error": str(error), "retryable": error.retryable, "outcomeUnknown": True}, status_code=503)


@app.exception_handler(TimeoutError)
async def operation_timeout(request, error):
    return JSONResponse({"error": "Booking operation timed out. Check the tool receipt before retrying.", "retryable": True, "outcomeUnknown": True}, status_code=504)


@app.exception_handler(HTTPException)
async def http_error(request, error):
    return JSONResponse({"error": error.detail}, status_code=error.status_code)


@app.exception_handler(RequestValidationError)
async def invalid_request(request, error):
    # Validation errors otherwise echo the supplied input, including keys/codes.
    return JSONResponse({"error": "Invalid request fields."}, status_code=400)


@lru_cache(maxsize=16)
def prepare_database(url, auth_token):
    store = Store(url, auth_token)
    try:
        store.init()
    finally:
        store.close()


def database():
    url, token = os.getenv("TURSO_DATABASE_URL") or "file:demo.sqlite", os.getenv("TURSO_AUTH_TOKEN", "")
    prepare_database(url, token)
    store = Store(url, token)
    try:
        yield store
    finally:
        store.close()


StoreDep = Annotated[Store, Depends(database)]


@app.get("/api/health")
def health():
    return {"status": "alive"}


@app.get("/api/ready")
def readiness(store: StoreDep):
    store.rows("SELECT 1")
    return {"status": "ready", "bookingWritesEnabled": os.getenv("BOOKING_WRITES_ENABLED", "true").lower() == "true"}


def account(request, store):
    caller = store.authenticate(request.cookies.get("session"))
    if not caller:
        raise HTTPException(401, "Start a call to open your private demo session.")
    return caller


def caller_card(store, caller):
    return {"name": caller["name"], "references": [{"reference": b["reference"], "trip": trip_label(b)} for b in store.list(caller["id"])]}


def limited_account(request, store):
    caller = account(request, store)
    store.limit("api:" + caller["id"], 100, 60_000)
    return caller


def client_address_hash(request):
    address = request.client.host if request.client else "unknown"
    return digest(address)


@app.post("/api/start")
def start(body: Body, request: Request, response: Response, store: StoreDep) -> dict:
    caller = store.authenticate(request.cookies.get("session"))
    if caller and (caller["name"] != PASSENGER or any(not b.get("cancellation") for b in store.list(caller["id"]))):
        caller = None
    if not caller:
        store.limit("start:" + client_address_hash(request), 12, 15 * 60_000)
        store.limit("new-callers", 100, 24 * 60 * 60_000)
        caller = store.create_caller()
        response.set_cookie("session", store.create_session(caller), max_age=86400, httponly=True, samesite="strict", secure=request.url.scheme == "https" or os.getenv("APP_ORIGIN", "").startswith("https://"))
    return {"caller": caller_card(store, caller)}


@app.get("/api/me")
def me(request: Request, store: StoreDep) -> dict:
    return {"caller": caller_card(store, account(request, store))}


@app.get("/api/inbox")
def inbox(request: Request, store: StoreDep) -> dict:
    account(request, store)
    return {"messages": store.inbox(digest(request.cookies["session"]))}


@app.post("/api/token")
def token(body: TokenBody, request: Request, store: StoreDep) -> dict:
    caller = limited_account(request, store)
    telemetry.annotate(call=digest(body.callId)[:16] if body.callId else None, reconnect=bool(body.callId))
    if body.callId:
        # Backstop for abandoned calls: never renew a voice session nobody has spoken in for CALL_IDLE_LIMIT_MS.
        with store.call(caller["id"], body.callId) as data:
            notes = data["state"].get("interactionNotes") or [{}]
            last = data["workflow"].get("lastCallerActivity") or notes[0].get("timestamp") or now_ms()
            idle = now_ms() - last > CALL_IDLE_LIMIT_MS
            if idle:
                data["ended"] = True
                data["workflow"]["stage"] = "ended"
        if idle:
            telemetry.annotate(outcome="idle_ended")
            return JSONResponse({"error": "This call ended because there was no response.", "ended": True, "retryable": False}, status_code=409)
    store.limit("token:" + caller["id"], 8, 60_000)
    store.limit("token-ip:" + client_address_hash(request), 12, 15 * 60_000)
    store.limit("daily-tokens", int(os.getenv("DEMO_DAILY_TOKEN_LIMIT", "100")), 24 * 60 * 60_000)
    if not os.getenv("GEMINI_API_KEY"):
        raise HTTPException(503, "The voice service is temporarily unavailable. Please try again later.")
    call_id = body.callId or store.new_call(caller["id"])
    telemetry.annotate(call=digest(call_id)[:16])
    with store.call(caller["id"], call_id) as data:
        state = data["state"]
    model = os.getenv("GEMINI_LIVE_MODEL", "gemini-3.8-live")
    config = live_config(body.handle)
    now = datetime.now(timezone.utc)
    with telemetry.span("gemini.token", kind=telemetry.SpanKind.CLIENT, model=model) as provider_span:
        try:
            with voice_provider.enter(), genai.Client(api_key=os.environ["GEMINI_API_KEY"], http_options={"api_version": "v1beta", "timeout": 15_000}) as ai:
                auth = ai.auth_tokens.create(config={"uses": 1, "expire_time": now + timedelta(minutes=30), "new_session_expire_time": now + timedelta(minutes=1), "live_connect_constraints": {"model": model, "config": config}})
        except ServiceBusy:
            provider_span.set_attribute("rejected", "circuit_open")
            raise
        except Exception as error:
            telemetry.mark_error(provider_span, error)
            raise ServiceBusy(30) from None
    return {"token": auth.name, "callId": call_id, "model": model, "config": config, "state": state}


@app.post("/api/turn")
def turn(body: TurnBody, request: Request, store: StoreDep) -> dict:
    caller = limited_account(request, store)
    event = {"kind": "turn", **body.model_dump(exclude={"key", "callId"})}
    output = process_event(store, caller["id"], digest(request.cookies["session"]), body.callId, event)
    verification = output["result"].get("verificationResult", {})
    approval = output["result"].get("approval") or {}
    telemetry.annotate(call=digest(body.callId)[:16], channel=body.channel, stage=output["workflow"]["stage"],
                       verification="sent" if verification.get("sent") else "waiting" if verification.get("notConfirmed") else None,
                       proposal=digest(body.proposalId)[:8] if body.proposalId else None,
                       declined=bool(approval.get("declined")) or None)
    logger.info(json.dumps({"event": "workflow_turn", "call": digest(body.callId)[:16], "turn": digest(body.turnId)[:16], "stage": output["workflow"]["stage"], "verification": "sent" if verification.get("sent") else "waiting" if verification.get("notConfirmed") else "none"}))
    return output


@app.post("/api/tool")
def tool(body: ToolBody, request: Request, store: StoreDep) -> dict:
    caller = limited_account(request, store)
    telemetry.annotate(call=digest(body.callId)[:16], tool=body.name)
    validate_tool(body.name, body.args)
    prior = store.tool_receipt(caller["id"], body.callId, body.requestId)
    if prior:
        if prior["fingerprint"] != "cancelled" and prior["fingerprint"] != digest(json.dumps([body.name, body.args], sort_keys=True)):
            raise ValueError("Tool request ID was reused.")
        rows = store.rows("SELECT data FROM calls WHERE id=? AND account_id=?", body.callId, caller["id"])
        telemetry.annotate(outcome="replayed")
        return {"result": prior["result"], "state": json.loads(rows[0]["data"])["state"]}
    # Authenticate the call before fetching external policy, outside the write transaction.
    with store.call(caller["id"], body.callId):
        pass
    policy = None
    if body.name == "lookup_policy":
        with telemetry.span("policy.lookup", kind=telemetry.SpanKind.CLIENT, topic=body.args["topic"]) as policy_span:
            policy = lookup_policy(body.args["topic"])
            policy_span.set_attribute("policyStatus", policy["status"])
            if policy["status"] == "unavailable":
                policy_span.set_status(telemetry.Status(telemetry.StatusCode.ERROR, "unavailable"))
    started = time.monotonic()
    try:
        output = process_event(store, caller["id"], digest(request.cookies["session"]), body.callId, body.model_dump(exclude={"key", "callId", "callerUtterance"}), policy)
    except ValueError as error:
        output = {"result": {"error": str(error)}}
    proposal = body.args.get("proposalId") or (output["result"].get("id") if body.name == "propose_action" else None)
    telemetry.annotate(outcome=telemetry.outcome_of(output["result"]), approvalStep=output["result"].get("approvalStep"),
                       stage=output.get("workflow", {}).get("stage"), action=body.args.get("action"),
                       proposal=digest(proposal)[:8] if isinstance(proposal, str) else None)
    logger.info(json.dumps({"event": "workflow_tool", "request": digest(body.requestId)[:16], "name": body.name, "elapsed_ms": int((time.monotonic() - started) * 1000), "outcome": "error" if output["result"].get("error") else "waiting" if output["result"].get("notConfirmed") else "ok",
                            "approvalStep": output["result"].get("approvalStep")}))
    return output


@app.post("/api/cancel")
def cancel(body: CancelBody, request: Request, store: StoreDep) -> dict:
    caller = limited_account(request, store)
    telemetry.annotate(call=digest(body.callId)[:16], cancelled=len(body.ids))
    with store.call(caller["id"], body.callId) as data:
        data["cancelled"] = list(dict.fromkeys(data["cancelled"] + body.ids))[-128:]
        for id in body.ids:
            if not store.tool_receipt(caller["id"], body.callId, id):
                store.save_tool_receipt(caller["id"], body.callId, id, "cancelled", {"cancelled": True})
        # Interruption invalidates outstanding approval, even before the tool ran.
        if body.ids:
            for p in data["state"]["proposals"]:
                if p["status"] == "pending":
                    p["status"] = "superseded"
    return {"ok": True}


@app.post("/api/tool-status")
def tool_status(body: ReceiptBody, request: Request, store: StoreDep) -> dict:
    caller = limited_account(request, store)
    # Ended calls must still expose their own committed receipts after handoff.
    rows = store.rows("SELECT data FROM calls WHERE id=? AND account_id=? AND expires>?", body.callId, caller["id"], int(time.time() * 1000))
    if not rows:
        raise ValueError("Call unavailable.")
    receipt = store.tool_receipt(caller["id"], body.callId, body.requestId)
    data = json.loads(rows[0]["data"])
    if receipt:
        return {"status": "cancelled" if receipt["fingerprint"] == "cancelled" else "completed", "result": receipt["result"], "state": data["state"]}
    return {"status": "cancelled" if body.requestId in data["cancelled"] else "unknown"}


@app.post("/api/end")
def end(body: CallBody, request: Request, store: StoreDep) -> dict:
    caller = limited_account(request, store)
    telemetry.annotate(call=digest(body.callId)[:16])
    with store.call(caller["id"], body.callId) as data:
        data["ended"] = True
        data["workflow"]["stage"] = "ended"
    return {"ok": True}


app.include_router(ops.router)

# Only built assets are public. Never mount the repository or its .env/database files.
if Path("dist").is_dir():
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory="dist", html=True), name="frontend")
