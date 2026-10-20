import unittest
from unittest.mock import patch
from resilience import ProviderGate, ServiceBusy, api_slots
from fastapi.testclient import TestClient
import telemetry
from app import app


class ResilienceTests(unittest.TestCase):
    def test_bulkhead_rejects_instead_of_queueing_and_releases(self):
        gate = ProviderGate(capacity=1)
        with gate.enter():
            with self.assertRaises(ServiceBusy):
                with gate.enter():
                    self.fail("Exceeded provider capacity")
        with gate.enter():
            pass

    def test_circuit_opens_and_allows_only_one_recovery_probe(self):
        gate = ProviderGate(failures=2, cooldown=30)
        with patch("resilience.time.monotonic", return_value=100):
            for _ in range(2):
                with self.assertRaises(TimeoutError):
                    with gate.enter():
                        raise TimeoutError()
            with self.assertRaises(ServiceBusy):
                with gate.enter():
                    self.fail("Circuit should be open")
        with patch("resilience.time.monotonic", return_value=131):
            with gate.enter():
                with self.assertRaises(ServiceBusy):
                    with gate.enter():
                        self.fail("Only one probe allowed")
            with gate.enter():
                pass
        self.assertEqual(gate.failures, 0)

    def test_late_success_cannot_close_open_circuit(self):
        gate = ProviderGate(failures=1)
        with gate.enter():
            with self.assertRaises(TimeoutError):
                with gate.enter():
                    raise TimeoutError()
        with self.assertRaises(ServiceBusy):
            with gate.enter():
                self.fail("Late success closed the circuit")

    def test_overload_is_not_retried_and_control_lane_survives(self):
        held = 0
        try:
            while api_slots.acquire(blocking=False):
                held += 1
            # No temporary database here: keep this test's telemetry out of the local demo database.
            with patch.object(telemetry, "submit"), TestClient(app) as client:
                response = client.get("/api/me")
                self.assertEqual(response.status_code, 503)
                self.assertFalse(response.json()["retryable"])
                self.assertEqual(response.headers["Retry-After"], "5")
                # A malformed control request reaches validation, not the saturated lane.
                response = client.get("/api/end")
                self.assertEqual(response.status_code, 404)
        finally:
            for _ in range(held):
                api_slots.release()
