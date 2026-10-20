from collections import deque
from concurrent.futures import Future
import os
from pathlib import Path
from threading import BoundedSemaphore
import tempfile
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from fastapi import HTTPException

from database import Store
import ops
import telemetry


class TelemetrySafetyTests(TestCase):
    def test_a_caller_cannot_attribute_telemetry_to_another_call(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "TURSO_DATABASE_URL": str(Path(temp) / "test.sqlite"), "TURSO_AUTH_TOKEN": ""
        }):
            store = Store()
            store.init()
            owner, other = store.create_caller(), store.create_caller()
            own_call, other_call = store.new_call(owner["id"]), store.new_call(other["id"])
            token = store.create_session(owner)
            store.close()
            with patch.object(telemetry, "submit") as submit:
                with self.assertRaises(HTTPException):
                    ops.ingest(ops.TelemetryBody(callId=other_call, spans=[]), SimpleNamespace(cookies={"session": token}))
                submit.assert_not_called()
                self.assertEqual(ops.ingest(ops.TelemetryBody(callId=own_call, spans=[]),
                                            SimpleNamespace(cookies={"session": token})), {"accepted": 0})

    def test_telemetry_overload_drops_batches_and_recovers_capacity(self):
        telemetry.drain()
        futures = []

        def enqueue(*args):
            future = Future()
            futures.append(future)
            return future

        with patch.object(telemetry, "_writer", SimpleNamespace(submit=enqueue)), \
             patch.object(telemetry, "_write_slots", BoundedSemaphore(2), create=True), \
             patch.object(telemetry, "_pending", set()), patch.object(telemetry, "_orphans", deque()):
            for _ in range(5):
                telemetry.submit(spans=[{"fixture": True}])
            self.assertEqual(len(futures), 2, "a stalled database must not grow an unbounded queue")
            futures[0].set_result(None)
            telemetry.submit(spans=[{"fixture": True}])
            self.assertEqual(len(futures), 3)
            for future in futures[1:]:
                future.set_result(None)
            self.assertFalse(telemetry._pending)
