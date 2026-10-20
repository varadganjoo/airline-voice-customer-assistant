"""Process-local admission and provider isolation; fleet capacity belongs at the gateway."""
from contextlib import contextmanager
from threading import BoundedSemaphore, Lock
import time
import logging


class ServiceBusy(Exception):
    def __init__(self, retry_after=5):
        self.retry_after = retry_after
        super().__init__("The service is temporarily busy. Please try again shortly.")


class ProviderGate:
    def __init__(self, capacity=4, failures=3, cooldown=30, name="dependency"):
        self.slots = BoundedSemaphore(capacity)
        self.lock = Lock()
        self.failures = 0
        self.threshold = failures
        self.cooldown = cooldown
        self.open_until = 0
        self.probing = False
        self.name = name

    @contextmanager
    def enter(self):
        with self.lock:
            if self.open_until > time.monotonic() or self.probing:
                raise ServiceBusy(max(1, int(self.open_until - time.monotonic()) + 1))
            probe = bool(self.open_until)
            if not self.slots.acquire(blocking=False):
                raise ServiceBusy()
            self.probing = probe
        try:
            yield
        except Exception:
            with self.lock:
                self.failures += 1
                if probe or self.failures >= self.threshold:
                    self.open_until = time.monotonic() + self.cooldown
                    logging.getLogger("airline.workflow").warning("provider_circuit_open dependency=%s cooldown_seconds=%s", self.name, self.cooldown)
            raise
        else:
            with self.lock:
                # A success already in flight must not close a newly opened circuit.
                if probe or not self.open_until:
                    self.failures = 0
                    self.open_until = 0
                    if probe:
                        logging.getLogger("airline.workflow").info("provider_circuit_recovered dependency=%s", self.name)
        finally:
            with self.lock:
                if probe:
                    self.probing = False
            self.slots.release()


api_slots = BoundedSemaphore(32)
recovery_slots = BoundedSemaphore(8)
voice_provider = ProviderGate(name="voice_token")
