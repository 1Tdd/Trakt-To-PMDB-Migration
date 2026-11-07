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
            self._entries[self._digest(token)] = (ProtectedCredential(api_key), self.clock() + self.ttl)
        return token

    def _entry(self, token):
        if not token or len(token) > 128:
            return None
        with self._lock:
            self._purge()
            entry = self._entries.get(self._digest(token))
            return entry[0] if entry else None

    def get(self, token):
        credential = self._entry(token)
        return credential.reveal() if credential else None

    def owner(self, token):
        credential = self._entry(token)
        return credential.owner_digest if credential else None

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
