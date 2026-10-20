"""Observability: OpenTelemetry traces, structured logs and derived metrics, stored with the app's own data.

* Traces: the OpenTelemetry SDK creates W3C trace/span IDs. A browser tool call sends a `traceparent` header, so
  one trace runs from the browser through the HTTP request, workflow nodes, database transactions and approval
  judges. Finished spans are buffered per request.
* Logs: records on the app's logger are captured with the active trace and span IDs, so a log line links to its trace.
* Metrics: rates, error ratios, latency percentiles and approval outcomes are derived from spans at query time
  (the span-metrics pattern), so no separate metrics pipeline is needed. See ops.py.

Telemetry never slows or fails a request: rows go to a single background writer thread. On serverless platforms a
frozen instance may delay or drop the last batch; that is acceptable for best-effort telemetry.
Nothing sensitive is recorded: no transcripts, verification codes, keys or booking contents, and call IDs are hashed.
"""

from collections import deque
from concurrent.futures import ThreadPoolExecutor
import contextvars
from functools import wraps
import json
import logging
import os
import random
import threading
import time

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

RETENTION_MS = 24 * 60 * 60_000
MAX_SPANS_PER_REQUEST = 500
_buffer = contextvars.ContextVar("telemetry_buffer", default=None)
_orphans = deque(maxlen=500)  # Logs emitted outside a request (startup, background threads).
_writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="telemetry")
_pending = set()
_pending_lock = threading.Lock()
_schema_ready = set()
_propagator = TraceContextTextMapPropagator()
_quiet = logging.getLogger("airline.telemetry")  # Telemetry's own problems go to stdout only, never back into telemetry.


def clean(attrs):
    """Keep only short primitive attribute values."""
    out = {}
    for key, value in attrs.items():
        if value is None:
            continue
        if isinstance(value, str):
            out[key] = value[:160]
        elif isinstance(value, (bool, int, float)):
            out[key] = value
    return out


def _span_row(span, service="airline-voice-api"):
    attrs = dict(span.attributes or {})
    for event in span.events or []:
        attrs.setdefault("events", "")
        attrs["events"] = (attrs["events"] + ("; " if attrs["events"] else "") + event.name)[:300]
    status = "error" if span.status.status_code == StatusCode.ERROR else "ok"
    return {"trace_id": format(span.context.trace_id, "032x"), "span_id": format(span.context.span_id, "016x"),
            "parent_id": format(span.parent.span_id, "016x") if span.parent else None, "name": span.name[:120],
            "kind": span.kind.name.lower(), "service": service, "start_ms": span.start_time / 1e6,
            "duration_ms": max(0.0, (span.end_time - span.start_time) / 1e6), "status": status,
            "call": attrs.pop("call", None), "attrs": json.dumps(clean(attrs))}


class _BufferProcessor(SpanProcessor):
    def on_start(self, span, parent_context=None):
        pass

    def on_end(self, span):
        buffer = _buffer.get()
        if buffer is not None and len(buffer["spans"]) < MAX_SPANS_PER_REQUEST:
            buffer["spans"].append(_span_row(span))

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=30_000):
        return True


provider = TracerProvider(resource=Resource.create({"service.name": "airline-voice-api"}))
provider.add_span_processor(_BufferProcessor())
tracer = provider.get_tracer("airline.voice")


class _LogCapture(logging.Handler):
    """Copies records into telemetry with the active trace and span, parsing the app's JSON log lines."""

    def emit(self, record):
        try:
            message = record.getMessage()
            try:
                parsed = json.loads(message)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                event, attrs = str(parsed.pop("event", record.name)), parsed
            else:
                event, attrs = message.split(" ", 1)[0][:80], {}
            context = trace.get_current_span().get_span_context()
            row = {"ts": record.created * 1000, "level": record.levelname.lower(), "event": event[:80], "message": message[:400],
                   "trace_id": format(context.trace_id, "032x") if context.is_valid else None,
                   "span_id": format(context.span_id, "016x") if context.is_valid else None,
                   "call": attrs.pop("call", None) if isinstance(attrs.get("call"), str) else None,
                   "attrs": json.dumps(clean(attrs))}
            buffer = _buffer.get()
            (buffer["logs"] if buffer is not None else _orphans).append(row)
        except Exception:
            pass


def install_log_capture(*logger_names):
    for name in logger_names:
        target = logging.getLogger(name)
        if not any(isinstance(h, _LogCapture) for h in target.handlers):
            target.addHandler(_LogCapture(level=logging.INFO))


def begin():
    """Start collecting telemetry for the current request."""
    buffer = {"spans": [], "logs": []}
    _buffer.set(buffer)
    return buffer


def extract(headers):
    """Parent context from a W3C traceparent header, if the browser sent one."""
    return _propagator.extract(carrier={"traceparent": headers.get("traceparent", "")}) if headers.get("traceparent") else None


def span(name, kind=SpanKind.INTERNAL, context=None, **attrs):
    return tracer.start_as_current_span(name, context=context, kind=kind, attributes=clean(attrs),
                                        record_exception=False, set_status_on_exception=False)


def annotate(**attrs):
    current = trace.get_current_span()
    if current.is_recording():
        current.set_attributes(clean(attrs))


def outcome_of(result):
    if not isinstance(result, dict):
        return "ok"
    for key, label in (("error", "error"), ("notConfirmed", "waiting"), ("done", "done"), ("verified", "verified"),
                       ("sent", "sent"), ("handoff", "handoff"), ("cancelled", "cancelled")):
        if result.get(key):
            return label
    return "ok"


def traced(name, fn):
    """Wrap a function (for example a LangGraph node) in a span; keeps its signature for frameworks that inspect it."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        with span(name) as current:
            try:
                output = fn(*args, **kwargs)
            except ValueError as error:  # Expected refusals: not a server error.
                current.set_attributes({"outcome": "rejected", "error.type": type(error).__name__})
                raise
            except Exception as error:
                current.set_status(Status(StatusCode.ERROR, type(error).__name__))
                current.set_attribute("error.type", type(error).__name__)
                raise
            result = output.get("result") if isinstance(output, dict) else None
            if isinstance(result, dict):
                current.set_attribute("outcome", outcome_of(result))
            return output
    return wrapper


def mark_error(current, error):
    current.set_status(Status(StatusCode.ERROR, type(error).__name__))
    current.set_attribute("error.type", type(error).__name__)


SPAN_COLUMNS = ("trace_id", "span_id", "parent_id", "name", "kind", "service", "start_ms", "duration_ms", "status", "call", "attrs")
LOG_COLUMNS = ("ts", "level", "event", "message", "trace_id", "span_id", "call", "attrs")


def ensure_schema(store, key=None):
    key = key or os.getenv("TURSO_DATABASE_URL") or "file:demo.sqlite"
    if key in _schema_ready:
        return
    for sql in [
        "CREATE TABLE IF NOT EXISTS telemetry_spans(trace_id TEXT NOT NULL, span_id TEXT NOT NULL, parent_id TEXT, name TEXT NOT NULL, kind TEXT, service TEXT, start_ms REAL NOT NULL, duration_ms REAL NOT NULL, status TEXT NOT NULL, call TEXT, attrs TEXT)",
        "CREATE INDEX IF NOT EXISTS telemetry_spans_start ON telemetry_spans(start_ms)",
        "CREATE INDEX IF NOT EXISTS telemetry_spans_trace ON telemetry_spans(trace_id)",
        "CREATE INDEX IF NOT EXISTS telemetry_spans_call ON telemetry_spans(call, start_ms)",
        "CREATE TABLE IF NOT EXISTS telemetry_logs(ts REAL NOT NULL, level TEXT NOT NULL, event TEXT, message TEXT, trace_id TEXT, span_id TEXT, call TEXT, attrs TEXT)",
        "CREATE INDEX IF NOT EXISTS telemetry_logs_ts ON telemetry_logs(ts)",
    ]:
        store.execute(sql)
    _schema_ready.add(key)


def _insert(store, table, columns, rows):
    for start in range(0, len(rows), 200):
        chunk = rows[start:start + 200]
        placeholders = ",".join("(" + ",".join("?" * len(columns)) + ")" for _ in chunk)
        store.execute(f"INSERT INTO {table}({','.join(columns)}) VALUES {placeholders}", *[row.get(c) for row in chunk for c in columns])


def _write(target, spans, logs):
    from database import Store  # Late import: database imports telemetry.
    _buffer.set(None)  # Spans created while writing telemetry are not themselves recorded.
    try:
        store = Store(*target)
        try:
            ensure_schema(store, target[0])
            if spans:
                _insert(store, "telemetry_spans", SPAN_COLUMNS, spans)
            if logs:
                _insert(store, "telemetry_logs", LOG_COLUMNS, logs)
            if random.random() < 0.05:
                cutoff = time.time() * 1000 - RETENTION_MS
                store.execute("DELETE FROM telemetry_spans WHERE start_ms < ?", cutoff)
                store.execute("DELETE FROM telemetry_logs WHERE ts < ?", cutoff)
        finally:
            store.close()
    except Exception as error:
        _quiet.warning("telemetry_write_failed %s", type(error).__name__)


def submit(spans=(), logs=()):
    """Queue rows for the background writer. Never blocks the request."""
    spans, logs = list(spans), list(logs)
    while _orphans:
        logs.append(_orphans.popleft())
    if not spans and not logs:
        return
    # The database is chosen now, on the request's thread, not later on the writer thread.
    target = (os.getenv("TURSO_DATABASE_URL") or "file:demo.sqlite", os.getenv("TURSO_AUTH_TOKEN", ""))
    future = _writer.submit(_write, target, spans, logs)
    with _pending_lock:
        _pending.add(future)
    future.add_done_callback(lambda f: _pending.discard(f))


def flush(buffer):
    submit(buffer["spans"], buffer["logs"])
    buffer["spans"], buffer["logs"] = [], []


def drain(timeout=5):
    """Wait for queued writes (tests and shutdown)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _pending_lock:
            if not _pending:
                return
        time.sleep(0.01)


install_log_capture("airline.workflow")
