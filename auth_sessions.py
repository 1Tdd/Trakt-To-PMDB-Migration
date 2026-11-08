"""Short-lived, process-local sessions; credentials never go in browser cookies."""

import hashlib
import secrets
import threading
import time
from credentials import ProtectedCredential

SESSION_TTL_SECONDS = 3600


class SessionStore:
    def __init__(self, ttl=SESSION_TTL_SECONDS, capacity=128, clock=time.monotonic):
        self.ttl = ttl
        self.capacity = capacity
        self.clock = clock
        self._entries = {}
        self._lock = threading.Lock()

    @staticmethod
    def _digest(token):
        return hashlib.sha256(token.encode()).digest()

    def _purge(self):
        now = self.clock()
        self._entries = {key: value for key, value in self._entries.items() if value[1] > now}

    def create(self, api_key):
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._purge()
            if len(self._entries) >= self.capacity:
                raise OverflowError("Session capacity reached")
            self._entries[self._digest(token)] = (ProtectedCredential(api_key), self.clock() + self.ttl, hashlib.sha256(api_key.encode()).hexdigest())
        return token

    def _entry(self, token):
        if not token or len(token) > 128:
            return None
        with self._lock:
            self._purge()
            entry = self._entries.get(self._digest(token))
            return entry if entry else None

    def get(self, token):
        credential = self._entry(token)
        return credential[0].reveal() if credential and credential[0] else None

    def owner(self, token):
        credential = self._entry(token)
        return credential[2] if credential else None

    def revoke(self, token):
        if token:
            with self._lock:
                self._entries.pop(self._digest(token), None)

    def purge(self):
        with self._lock:
            self._purge()

    def clear(self):
        with self._lock:
            self._entries.clear()

    def forget_owner_key(self, owner):
        """Keep identity-only access to results, never a reusable credential."""
        with self._lock:
            for key, entry in list(self._entries.items()):
                if secrets.compare_digest(entry[2], owner):
                    self._entries[key] = (None, entry[1], entry[2])

    def status(self, token):
        entry = self._entry(token)
        return {"pmdb": bool(entry and entry[0]), "can_view_results": bool(entry),
                "expires_in": max(0, int(entry[1] - self.clock())) if entry else 0}
