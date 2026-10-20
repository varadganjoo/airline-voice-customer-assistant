"""Observability: browser-to-database traces, derived metrics, log correlation, privacy and failure isolation."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app import app, prepare_database
from database import Store, digest
import ops
import telemetry

BROWSER_TRACE, BROWSER_SPAN = "4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7"


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="airline-telemetry-test-")
        self.url = str(Path(self.temp.name) / "test.sqlite")
        self.env = patch.dict(os.environ, {"TURSO_DATABASE_URL": self.url, "TURSO_AUTH_TOKEN": "", "GEMINI_API_KEY": "", "ACCESS_KEY": "", "APP_ORIGIN": ""})
        self.env.start()
        self.client = TestClient(app).__enter__()
        self.post("start", {})
        store = Store(self.url)
        self.call = store.new_call(store.authenticate(self.client.cookies["session"])["id"])
        store.close()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        telemetry.drain()
        prepare_database.cache_clear()
        self.env.stop()
        self.temp.cleanup()

    def post(self, route, body, **headers):
        return self.client.post("/api/" + route, headers={"origin": "http://testserver", **headers}, json=body)

    def all_rows(self):
        telemetry.drain()
        store = Store(self.url)
        try:
            return store.rows("SELECT * FROM telemetry_spans"), store.rows("SELECT * FROM telemetry_logs")
        finally:
            store.close()

    def test_browser_trace_continues_through_workflow_and_database(self):
        response = self.post("tool", {"callId": self.call, "requestId": str(uuid4()), "name": "save_notes", "args": {"notes": "Change request."}},
                             traceparent=f"00-{BROWSER_TRACE}-{BROWSER_SPAN}-01")
        self.assertEqual(response.status_code, 200)
        self.assertIn(BROWSER_TRACE, response.headers["traceresponse"])
        telemetry.drain()
        trace = self.client.get(f"/api/ops/trace/{BROWSER_TRACE}").json()
        spans = {s["name"]: s for s in trace["spans"]}
        server = spans["POST /api/tool"]
        self.assertEqual(server["parent_id"], BROWSER_SPAN, "server span is a child of the browser span")
        self.assertEqual(server["attrs"]["tool"], "save_notes")
        self.assertEqual(server["call"], digest(self.call)[:16])
        for name in ("workflow.graph", "workflow.guard", "workflow.receipt", "db.transaction"):
            self.assertIn(name, spans)
        self.assertTrue(any(log["event"] == "workflow_tool" for log in trace["logs"]), "logs carry the trace ID")

    def test_metrics_traces_and_logs_endpoints(self):
        for text in ("I'd like to change my flight", "Michael Jackson"):
            self.post("turn", {"callId": self.call, "turnId": str(uuid4()), "text": text})
        self.post("tool", {"callId": self.call, "requestId": str(uuid4()), "name": "save_notes", "args": {"notes": "Caller wants a change."}})
        telemetry.drain()
        summary = self.client.get("/api/ops/summary?window=15m").json()
        self.assertGreaterEqual(summary["kpis"]["requests"], 4)
        self.assertEqual(summary["kpis"]["calls"], 1)
        self.assertIn("/api/turn", {r["route"] for r in summary["routes"]})
        self.assertEqual(len(summary["series"]), 30)
        self.assertEqual(sum(b["requests"] for b in summary["series"]), summary["kpis"]["requests"])
        traces = self.client.get("/api/ops/traces?window=15m&q=save_notes").json()["traces"]
        self.assertEqual([t["tool"] for t in traces], ["save_notes"])
        self.assertTrue(self.client.get("/api/ops/logs?window=15m&q=workflow_turn").json()["logs"])
        calls = self.client.get("/api/ops/calls?window=15m").json()["calls"]
        self.assertEqual(calls[0]["turns"], 2)
        self.assertEqual(self.client.get("/api/ops/summary?window=2d").status_code, 400)
        self.assertEqual(self.client.get("/api/ops/trace/not-a-trace").status_code, 400)

    def test_nothing_sensitive_is_recorded(self):
        self.post("turn", {"callId": self.call, "turnId": str(uuid4()), "text": "My code is 482913 and my name is Michael Jackson"})
        self.post("tool", {"callId": self.call, "requestId": str(uuid4()), "name": "save_notes", "args": {"notes": "SECRETNOTE caller details"}})
        spans, logs = self.all_rows()
        dump = json.dumps([spans, logs])
        self.assertTrue(spans)
        for secret in ("482913", "SECRETNOTE", "Michael", self.client.cookies["session"], self.call):
            self.assertNotIn(secret, dump)

    def test_browser_span_intake_is_validated_and_authenticated(self):
        span = {"traceId": BROWSER_TRACE, "spanId": BROWSER_SPAN, "name": "browser.voice_latency", "startMs": 1, "durationMs": 820, "attrs": {"turn": 1}}
        self.assertEqual(self.post("telemetry", {"callId": self.call, "spans": [span]}).json(), {"accepted": 1})
        self.assertEqual(self.post("telemetry", {"spans": [dict(span, name="server.fake")]}).status_code, 400)
        self.assertEqual(self.post("telemetry", {"spans": [dict(span, attrs={"x": {"nested": 1}})]}).status_code, 400)
        self.assertEqual(self.post("telemetry", {"spans": [span] * 51}).status_code, 400)
        self.client.cookies.clear()
        self.assertEqual(self.post("telemetry", {"spans": [span]}).status_code, 401)
        spans, _ = self.all_rows()
        voice = [s for s in spans if s["name"] == "browser.voice_latency"]
        self.assertEqual(len(voice), 1)
        self.assertEqual((voice[0]["service"], voice[0]["call"]), ("browser", digest(self.call)[:16]))
        self.assertGreater(voice[0]["start_ms"], 1, "browser timestamps are clamped to the last hour")

    def test_call_session_view_joins_server_and_voice_model_events(self):
        self.post("turn", {"callId": self.call, "turnId": str(uuid4()), "text": "I need to change my flight"})
        self.post("tool", {"callId": self.call, "requestId": str(uuid4()), "name": "save_notes", "args": {"notes": "Change request."}})
        span = lambda name, **attrs: {"traceId": uuid4().hex, "spanId": uuid4().hex[:16], "name": name, "startMs": ops.now_ms(), "durationMs": 900, "attrs": attrs}
        self.post("telemetry", {"callId": self.call, "spans": [
            span("browser.agent_turn", inputTokens=6000, outputTokens=200), span("browser.agent_turn", inputTokens=6500, outputTokens=40, interrupted=True),
            span("browser.interrupted", agentSpokeMs=1200), span("browser.agent_turn", placeholder=True), span("browser.reply_nudge"), span("browser.call_end", reason="caller_hung_up")]})
        telemetry.drain()
        call = digest(self.call)[:16]
        detail = self.client.get(f"/api/ops/call/{call}").json()
        summary = detail["summary"]
        self.assertEqual((summary["turns"], summary["agentTurns"], summary["interruptions"], summary["nudges"], summary["placeholders"]), (1, 3, 1, 1, 1))
        self.assertEqual(summary["tokens"]["live"], {"input": 12500, "output": 240, "responses": 3})
        self.assertEqual(summary["ended"], "caller_hung_up")
        self.assertIn("identify", summary["stages"])
        names = [e["name"] for e in detail["events"]]
        self.assertIn("POST /api/turn", names)
        self.assertIn("browser.agent_turn", names)
        self.assertEqual(self.client.get("/api/ops/calls?window=15m").json()["calls"][0]["tokens"]["live"]["output"], 240)
        self.assertEqual(self.client.get("/api/ops/call/not-a-call").status_code, 400)
        self.assertEqual(self.client.get("/api/ops/call/0123456789abcdef").status_code, 404)

    def test_approval_ladder_is_reconstructed_per_quote(self):
        server = lambda trace, t, **attrs: {"trace_id": trace, "start_ms": t, "attrs": attrs}
        spans = [server("t1", 1, tool="propose_action", proposal="p1", action="add_bags"),
                 server("t2", 2, **{"http.route": "/api/turn", "channel": "voice", "proposal": "p1"}),
                 server("t3", 3, tool="confirm_action", proposal="p1", outcome="waiting", approvalStep="manual"),
                 server("t4", 4, **{"http.route": "/api/turn", "channel": "keypad", "proposal": "p1"}),
                 server("t5", 5, tool="confirm_action", proposal="p1", outcome="done"),
                 server("t6", 6, tool="propose_action", proposal="p2", action="change_flight"),
                 server("t7", 7, **{"http.route": "/api/turn", "channel": "voice", "proposal": "p2", "declined": True})]
        quotes = ops._approvals(spans, {"t2": {"decision": "unclear", "mode": "closed"}})
        self.assertEqual([(q["proposal"], q["result"], q["reachedKeypad"]) for q in quotes], [("p1", "committed", True), ("p2", "declined", False)])
        self.assertEqual([s["step"] for s in quotes[0]["steps"]], ["Quoted add bags", "Caller replied; judges decided: unclear",
                                                                    "Not committed; next approval step: manual", "Caller pressed 1 to confirm", "Approved and committed"])

    def test_telemetry_failure_never_breaks_a_request(self):
        with patch.object(telemetry, "_insert", side_effect=RuntimeError("database down")):
            response = self.post("turn", {"callId": self.call, "turnId": str(uuid4()), "text": "hello"})
            telemetry.drain()
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
