"""Cooperative cancellation and lifetime limits, isolated per worker thread."""
import threading
import time

MAX_JOB_SECONDS = 6 * 60 * 60
_worker = threading.local()


class MigrationStopped(Exception):
    pass


class JobControl:
    def __init__(self, max_seconds=MAX_JOB_SECONDS):
        self.cancelled = threading.Event()
        self.deadline = time.monotonic() + max_seconds

    def check(self):
        if self.cancelled.is_set():
            raise MigrationStopped("Migration stopped by the user.")
        if time.monotonic() >= self.deadline:
            raise MigrationStopped("Migration stopped after reaching its 6-hour time limit.")


def bind(control):
    _worker.control = control


def checkpoint():
    control = getattr(_worker, "control", None)
    if control:
        control.check()
