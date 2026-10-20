"""Observability dashboard API: metrics derived from spans, trace and call explorers, logs, and browser telemetry intake.

Read endpoints require a separate operator credential. They return aggregates, span names, timings
and hashed call IDs only; telemetry never contains transcripts, codes, keys or booking contents.
"""

from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import time
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from database import Store, digest
import telemetry

router = APIRouter()
WINDOWS = {"15m": 15, "1h": 60, "6h": 360, "24h": 1440}
WindowParam = Annotated[Literal["15m", "1h", "6h", "24h"], Query()]


def now_ms():
    return time.time() * 1000


def pct(values, q):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))], 1)


def _store():
    store = Store()
    telemetry.ensure_schema(store)
    return store


def _spans(store, since, columns="trace_id,span_id,parent_id,name,kind,service,start_ms,duration_ms,status,call,attrs", limit=40_000):
    rows = store.rows(f"SELECT {columns} FROM telemetry_spans WHERE start_ms >= ? ORDER BY start_ms DESC LIMIT ?", since, limit)
    for row in rows:
        row["attrs"] = json.loads(row["attrs"] or "{}")
    return rows


def _is_server_error(span):
    return span["status"] == "error" or (span["attrs"].get("http.status_code") or 0) >= 500


@router.get("/ops", include_in_schema=False)
def ops_page():
    page = Path("dist/ops.html")
    if not page.is_file():
        raise HTTPException(404, "Run npm run build first.")
    return FileResponse(page, headers={"Cache-Control": "no-store"})


@router.get("/api/ops/summary")
def summary(window: WindowParam = "1h") -> dict:
    span_ms = WINDOWS[window] * 60_000
    end, since = now_ms(), now_ms() - span_ms
    store = _store()
    try:
        spans = _spans(store, since)
        logs = store.rows("SELECT ts,event,message FROM telemetry_logs WHERE ts >= ? AND event LIKE 'provider_circuit_%' ORDER BY ts", since)
    finally:
        store.close()
    server = [s for s in spans if s["kind"] == "server"]
    browser = [s for s in spans if s["service"] == "browser"]
    by_name = defaultdict(list)
    for s in spans:
        by_name[s["name"]].append(s)

    # RED metrics per route.
    routes = defaultdict(list)
    for s in server:
        routes[s["attrs"].get("http.route", s["name"])].append(s)
    route_rows = sorted(({"route": r, "count": len(v), "errors": sum(map(_is_server_error, v)),
                          "p50": pct([x["duration_ms"] for x in v], .5), "p95": pct([x["duration_ms"] for x in v], .95)}
                         for r, v in routes.items()), key=lambda r: -r["count"])

    # Tools: calls, outcomes and latency, from the server span of each /api/tool request.
    tools = defaultdict(list)
    for s in server:
        if s["attrs"].get("tool"):
            tools[s["attrs"]["tool"]].append(s)
    tool_rows = sorted(({"tool": t, "count": len(v), "outcomes": dict(Counter(x["attrs"].get("outcome", "ok") for x in v)),
                         "p95": pct([x["duration_ms"] for x in v], .95)} for t, v in tools.items()), key=lambda r: -r["count"])

    confirms = tools.get("confirm_action", [])
    turns = [s for s in server if s["attrs"].get("http.route") == "/api/turn"]
    approvals = {"approved": sum(s["attrs"].get("outcome") == "done" for s in confirms),
                 "heldForClarity": sum(s["attrs"].get("approvalStep") in ("natural", "closed") for s in confirms),
                 "reachedKeypad": sum(s["attrs"].get("approvalStep") == "manual" for s in confirms),
                 "keypadPresses": sum(s["attrs"].get("channel") == "keypad" for s in turns),
                 "declined": sum(bool(s["attrs"].get("declined")) for s in confirms + turns)}
    judges = by_name.get("consent.judge", [])
    verify = tools.get("verify_booking", [])
    voice = [s["duration_ms"] for s in by_name.get("browser.voice_latency", [])]
    calls = {s["call"] for s in spans if s["call"]}
    ended = {s["call"] for s in by_name.get("browser.call_end", []) if s["call"]}
    recent = {s["call"] for s in spans if s["call"] and s["start_ms"] >= end - 120_000}

    # Dependency health: circuit-breaker events plus failing dependency spans.
    dependencies = {}
    for name, span_name in (("voice_token", "gemini.token"), ("policy", "policy.lookup"), ("consent", "consent.judge"), ("database", "db.transaction")):
        group = by_name.get(span_name, [])
        failed = sum(1 for s in group if s["status"] == "error" or s["attrs"].get("rejected") in ("unavailable", "timeout"))
        dependencies[name] = {"calls": len(group), "failures": failed, "p95": pct([s["duration_ms"] for s in group], .95), "circuit": "closed"}
    for log in logs:
        match = re.search(r"dependency=(\w+)", log["message"] or "")
        if match and match.group(1) in dependencies:
            open_until = log["ts"] + 30_000
            dependencies[match.group(1)]["circuit"] = "open" if log["event"] == "provider_circuit_open" and open_until > end else "closed"
            dependencies[match.group(1)]["lastCircuitEvent"] = log["ts"]

    # Time series, 30 buckets across the window.
    buckets = 30
    size = span_ms / buckets
    series = [{"t": since + i * size, "requests": 0, "errors": 0, "lat": [], "voice": [], "tools": 0} for i in range(buckets)]
    for s in server:
        b = series[min(buckets - 1, int((s["start_ms"] - since) // size))]
        b["requests"] += 1
        b["errors"] += _is_server_error(s)
        b["lat"].append(s["duration_ms"])
        b["tools"] += bool(s["attrs"].get("tool"))
    for s in by_name.get("browser.voice_latency", []):
        series[min(buckets - 1, int((s["start_ms"] - since) // size))]["voice"].append(s["duration_ms"])
    for b in series:
        b["p50"], b["p95"], b["voiceP50"] = pct(b["lat"], .5), pct(b.pop("lat"), .95), pct(b.pop("voice"), .5)

    latencies = [s["duration_ms"] for s in server]
    return {
        "window": window, "generatedAt": end, "bucketMs": size,
        "kpis": {"requests": len(server), "serverErrors": sum(map(_is_server_error, server)),
                 "rateLimited": sum(s["attrs"].get("http.status_code") == 429 for s in server),
                 "busy": sum(s["attrs"].get("http.status_code") == 503 for s in server),
                 "p50": pct(latencies, .5), "p95": pct(latencies, .95), "p99": pct(latencies, .99),
                 "voiceP50": pct(voice, .5), "voiceP95": pct(voice, .95), "voiceSamples": len(voice),
                 "calls": len(calls), "activeCalls": len(recent - ended), "toolCalls": sum(r["count"] for r in tool_rows),
                 "verified": sum(s["attrs"].get("outcome") == "verified" for s in verify),
                 "verifyAttempts": sum(s["attrs"].get("outcome") in ("verified", "error") for s in verify),
                 "handoffs": sum(s["attrs"].get("outcome") == "handoff" for s in tools.get("transfer_to_representative", [])),
                 "idleHangups": sum(s["attrs"].get("reason") == "no_response" for s in by_name.get("browser.call_end", [])),
                 "reconnects": len(by_name.get("browser.reconnect", [])),
                 "placeholders": sum(bool(s["attrs"].get("placeholder")) for s in by_name.get("browser.agent_turn", [])),
                 "nudges": len(by_name.get("browser.reply_nudge", [])),
                 "judgeCalls": len(judges), "judgeP95": pct([s["duration_ms"] for s in judges], .95),
                 "browserSpans": len(browser)},
        "approvals": approvals,
        "judgeDecisions": dict(Counter(s["attrs"].get("decision", "unknown") for s in judges)),
        "routes": route_rows, "tools": tool_rows, "dependencies": dependencies, "series": series,
    }


@router.get("/api/ops/traces")
def traces(window: WindowParam = "1h", q: str = Query("", max_length=80), errors: bool = False,
           call: str = Query("", max_length=40)) -> dict:
    store = _store()
    try:
        spans = _spans(store, now_ms() - WINDOWS[window] * 60_000)
    finally:
        store.close()
    grouped = defaultdict(list)
    for s in spans:
        grouped[s["trace_id"]].append(s)
    out = []
    for trace_id, group in grouped.items():
        ids = {s["span_id"] for s in group}
        roots = [s for s in group if not s["parent_id"] or s["parent_id"] not in ids] or group
        root = min(roots, key=lambda s: s["start_ms"])
        start, finish = min(s["start_ms"] for s in group), max(s["start_ms"] + s["duration_ms"] for s in group)
        errs = sum(_is_server_error(s) for s in group)
        tool = next((s["attrs"].get("tool") for s in group if s["attrs"].get("tool")), None)
        item = {"traceId": trace_id, "name": root["name"], "service": root["service"], "start": start, "durationMs": round(finish - start, 1),
                "spans": len(group), "errors": errs, "call": next((s["call"] for s in group if s["call"]), None), "tool": tool,
                "outcome": next((s["attrs"].get("outcome") for s in group if s["kind"] == "server" and s["attrs"].get("outcome")), None),
                "status": next((s["attrs"].get("http.status_code") for s in group if s["kind"] == "server"), None)}
        text = " ".join(str(v) for v in (item["name"], item["tool"], item["call"], item["outcome"]) if v).lower()
        if (q and q.lower() not in text) or (errors and not errs) or (call and item["call"] != call):
            continue
        out.append(item)
    out.sort(key=lambda t: -t["start"])
    return {"traces": out[:150]}


@router.get("/api/ops/trace/{trace_id}")
def trace_detail(trace_id: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", trace_id):
        raise HTTPException(400, "Invalid trace ID.")
    store = _store()
    try:
        spans = store.rows("SELECT trace_id,span_id,parent_id,name,kind,service,start_ms,duration_ms,status,call,attrs FROM telemetry_spans WHERE trace_id=? ORDER BY start_ms", trace_id)
        logs = store.rows("SELECT ts,level,event,message,span_id FROM telemetry_logs WHERE trace_id=? ORDER BY ts", trace_id)
    finally:
        store.close()
    for s in spans:
        s["attrs"] = json.loads(s["attrs"] or "{}")
    return {"traceId": trace_id, "spans": spans, "logs": logs}


@router.get("/api/ops/logs")
def logs(window: WindowParam = "1h", level: Literal["", "info", "warning", "error"] = "", q: str = Query("", max_length=80)) -> dict:
    sql, args = "SELECT ts,level,event,message,trace_id,call FROM telemetry_logs WHERE ts >= ?", [now_ms() - WINDOWS[window] * 60_000]
    if level:
        sql += " AND level = ?"
        args.append(level)
    if q:
        sql += " AND (event LIKE ? OR message LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    store = _store()
    try:
        return {"logs": store.rows(sql + " ORDER BY ts DESC LIMIT 300", *args)}
    finally:
        store.close()


def _count(spans, name):
    return sum(s["name"] == name for s in spans)


def _total(spans, key):
    return sum(int(s["attrs"].get(key) or 0) for s in spans)


def _call_summary(call, own, inner):
    """One call (AI session): own = spans tagged with the call; inner = the other spans in its server traces."""
    server = sorted((s for s in own if s["kind"] == "server"), key=lambda s: s["start_ms"])
    tools = [s for s in server if s["attrs"].get("tool")]
    end = next((s for s in own if s["name"] == "browser.call_end"), None)
    start, finish = min(s["start_ms"] for s in own), max(s["start_ms"] + s["duration_ms"] for s in own)
    agent_turns = [s for s in own if s["name"] == "browser.agent_turn"]
    judges = [s for s in inner if s["name"] == "consent.judge"]
    voice = [s["duration_ms"] for s in own if s["name"] == "browser.voice_latency"]
    return {
        "call": call, "start": start, "durationMs": round(finish - start), "requests": len(server),
        "turns": sum(s["attrs"].get("http.route") == "/api/turn" for s in server), "tools": len(tools),
        "changes": sum(s["attrs"].get("tool") == "confirm_action" and s["attrs"].get("outcome") == "done" for s in tools),
        "errors": sum(map(_is_server_error, server)),
        "verified": any(s["attrs"].get("tool") == "verify_booking" and s["attrs"].get("outcome") == "verified" for s in tools),
        "stages": list(dict.fromkeys(s["attrs"]["stage"] for s in server if s["attrs"].get("stage"))),
        "voiceP50": pct(voice, .5), "voiceP95": pct(voice, .95), "agentTurns": len(agent_turns),
        "interruptions": _count(own, "browser.interrupted"), "nudges": _count(own, "browser.reply_nudge"),
        "placeholders": sum(bool(s["attrs"].get("placeholder")) for s in agent_turns),
        "idleCheckins": _count(own, "browser.idle_checkin"), "reconnects": _count(own, "browser.reconnect"),
        "refusalsHeld": _count(own, "browser.refusal_held"),
        "toolsCancelled": _total([s for s in own if s["name"] == "browser.tool_cancelled"], "count"),
        "handoff": any(s["attrs"].get("outcome") == "handoff" for s in tools),
        "tokens": {"live": {"input": _total(agent_turns, "inputTokens"), "output": _total(agent_turns, "outputTokens"), "responses": len(agent_turns)},
                   "judges": {"input": _total(judges, "inputTokens"), "output": _total(judges, "outputTokens"), "calls": len(judges)}},
        "ended": end["attrs"].get("reason") if end else ("active" if finish > now_ms() - 120_000 else "unknown"),
    }


@router.get("/api/ops/calls")
def calls(window: WindowParam = "1h") -> dict:
    store = _store()
    try:
        spans = _spans(store, now_ms() - WINDOWS[window] * 60_000)
    finally:
        store.close()
    grouped, trace_call = defaultdict(list), {}
    for s in spans:
        if s["call"]:
            grouped[s["call"]].append(s)
            trace_call[s["trace_id"]] = s["call"]
    inner = defaultdict(list)
    for s in spans:
        if not s["call"] and s["trace_id"] in trace_call:
            inner[trace_call[s["trace_id"]]].append(s)
    out = sorted((_call_summary(call, group, inner[call]) for call, group in grouped.items()), key=lambda c: -c["start"])
    return {"calls": out[:100]}


def _approval_step(attrs, judged):
    tool = attrs.get("tool")
    if tool == "propose_action":
        return "quoted", f"Quoted {(attrs.get('action') or 'change').replace('_', ' ')}"
    if tool == "confirm_action":
        outcome = attrs.get("outcome")
        if outcome == "done":
            return "committed", "Approved and committed"
        if outcome == "waiting":
            return "held", f"Not committed; next approval step: {attrs.get('approvalStep') or 'ask again'}"
        return outcome or "error", f"Commit {outcome or 'failed'}"
    if tool == "withdraw_quote":
        return "withdrawn", "Quote withdrawn"
    if attrs.get("http.route") != "/api/turn":
        return None, None
    if attrs.get("channel") == "keypad":
        return ("declined", "Caller pressed 7 to cancel") if attrs.get("declined") else ("keypad", "Caller pressed 1 to confirm")
    if attrs.get("declined"):
        return "declined", "Caller declined"
    return "reply", f"Caller replied; judges decided: {judged['decision']}" if judged else "Caller replied"


def _approvals(server, assessed):
    """The approval ladder for each quote in the call, in order: quoted, each judged reply or key press, and the result."""
    quotes = defaultdict(list)
    for s in server:
        proposal = s["attrs"].get("proposal")
        judged = assessed.get(s["trace_id"])
        kind, text = _approval_step(s["attrs"], judged)
        if proposal and kind:
            quotes[proposal].append({"t": s["start_ms"], "kind": kind, "step": text, "traceId": s["trace_id"], "mode": (judged or {}).get("mode")})
    out = []
    for proposal, steps in quotes.items():
        kinds = {x["kind"] for x in steps}
        result = next((k for k in ("committed", "declined", "withdrawn") if k in kinds), "pending")
        out.append({"proposal": proposal, "result": result, "steps": steps,
                    "reachedKeypad": "keypad" in kinds or any("manual" in x["step"] for x in steps)})
    return sorted(out, key=lambda q: q["steps"][0]["t"])


@router.get("/api/ops/call/{call}")
def call_detail(call: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{16}", call):
        raise HTTPException(400, "Invalid call ID.")
    store = _store()
    try:
        own = store.rows("SELECT trace_id,span_id,parent_id,name,kind,service,start_ms,duration_ms,status,call,attrs FROM telemetry_spans WHERE call=? ORDER BY start_ms LIMIT 5000", call)
        traces = sorted({s["trace_id"] for s in own if s["kind"] == "server"})
        inner = []
        for i in range(0, len(traces), 400):
            chunk = traces[i:i + 400]
            inner += store.rows(f"SELECT trace_id,name,start_ms,duration_ms,status,attrs FROM telemetry_spans WHERE call IS NULL AND trace_id IN ({','.join('?' * len(chunk))})", *chunk)
    finally:
        store.close()
    if not own:
        raise HTTPException(404, "No telemetry for this call.")
    for s in own + inner:
        s["attrs"] = json.loads(s["attrs"] or "{}")
    by_trace = defaultdict(list)
    for s in inner:
        by_trace[s["trace_id"]].append(s)
    assessed = {t: {"decision": s["attrs"]["decision"], "kind": s["attrs"].get("kind"), "mode": s["attrs"].get("mode")}
                for t, group in by_trace.items() for s in group if s["name"] == "workflow.assess" and s["attrs"].get("decision")}
    events = []
    for s in own:
        if s["service"] == "browser" and s["kind"] == "client":
            continue  # The browser side of an API call; its server span carries the detail.
        event = {"t": s["start_ms"], "name": s["name"], "service": s["service"], "durationMs": s["duration_ms"], "status": s["status"],
                 "traceId": s["trace_id"], "attrs": s["attrs"]}
        if s["kind"] == "server":
            event["assessment"] = assessed.get(s["trace_id"])
            event["judges"] = [{"role": j["attrs"].get("role"), "model": j["attrs"].get("model"), "decision": j["attrs"].get("decision"),
                                "rejected": j["attrs"].get("rejected"), "durationMs": j["duration_ms"]}
                               for j in by_trace[s["trace_id"]] if j["name"] == "consent.judge"]
            event["dependencies"] = [{"name": d["name"], "durationMs": d["duration_ms"], "status": d["status"]}
                                     for d in by_trace[s["trace_id"]] if d["name"] in ("gemini.token", "policy.lookup")]
        events.append(event)
    server = [s for s in own if s["kind"] == "server"]
    return {"summary": _call_summary(call, own, inner), "events": events, "approvals": _approvals(server, assessed)}


AttrValue = str | int | float | bool


class BrowserSpan(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    traceId: str = Field(pattern=r"^[0-9a-f]{32}$")
    spanId: str = Field(pattern=r"^[0-9a-f]{16}$")
    parentId: str | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")
    name: str = Field(pattern=r"^browser\.[a-z_.]{1,48}$")
    kind: Literal["client", "internal"] = "internal"
    startMs: float
    durationMs: float = Field(ge=0, le=600_000)
    status: Literal["ok", "error"] = "ok"
    attrs: dict[Annotated[str, Field(max_length=40)], AttrValue] = Field(default_factory=dict, max_length=16)


class TelemetryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    callId: str | None = Field(default=None, max_length=80)
    spans: list[BrowserSpan] = Field(max_length=50)


@router.post("/api/telemetry")
def ingest(body: TelemetryBody, request: Request) -> dict:
    store = Store()
    try:
        caller = store.authenticate(request.cookies.get("session"))
        if not caller:
            raise HTTPException(401, "Start a call first.")
        # ASVS 8.2.2: a caller may attach telemetry only to a call they own.
        if body.callId and not store.rows("SELECT id FROM calls WHERE id=? AND account_id=? AND expires>?",
                                         body.callId, caller["id"], now_ms()):
            raise HTTPException(404, "Call unavailable.")
        store.limit("telemetry:" + caller["id"], 120, 60_000)
    finally:
        store.close()
    call = digest(body.callId)[:16] if body.callId else None
    current = now_ms()
    rows = [{"trace_id": s.traceId, "span_id": s.spanId, "parent_id": s.parentId, "name": s.name, "kind": s.kind, "service": "browser",
             "start_ms": min(max(s.startMs, current - 3_600_000), current), "duration_ms": s.durationMs, "status": s.status,
             "call": call, "attrs": json.dumps(telemetry.clean(s.attrs))} for s in body.spans]
    telemetry.submit(spans=rows)
    return {"accepted": len(rows)}
