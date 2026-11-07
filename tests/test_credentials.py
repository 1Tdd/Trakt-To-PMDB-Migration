import os
os.environ["domain"] = "https://migration.test"
import subprocess
import sys
import unittest
from unittest.mock import patch
import requests
from cryptography.fernet import InvalidToken
from auth_sessions import SessionStore
from credentials import ProtectedCredential, ProtectedHeaders
import webserver

KEY = "synthetic-credential-security-test"

class CredentialTests(unittest.TestCase):
    def test_store_and_worker_headers_retain_ciphertext_only(self):
        store = SessionStore()
        token = store.create(KEY)
        self.assertNotIn(KEY, repr(store._entries))
        credential = next(iter(store._entries.values()))[0]
        self.assertNotIn(KEY.encode(), credential.ciphertext)
        headers = ProtectedHeaders(KEY)
        self.assertNotIn(KEY, repr(vars(headers)))
        self.assertEqual(store.get(token), KEY)
        prepared = requests.Request("GET", "https://example.test", headers=headers).prepare()
        self.assertEqual(prepared.headers["Authorization"], "Bearer " + KEY)

    def test_owner_check_never_decrypts(self):
        store = SessionStore()
        token = store.create(KEY)
        with patch.object(ProtectedCredential, "reveal", side_effect=AssertionError("Unexpected decryption")):
            self.assertIsNotNone(store.owner(token))
            store.revoke(token)
            self.assertIsNone(store.owner(token))

    def test_ciphertext_tampering_is_rejected(self):
        credential = ProtectedCredential(KEY)
        credential.ciphertext = b"invalid"
        with self.assertRaises(InvalidToken):
            credential.reveal()

    def test_running_job_registry_contains_only_owner_digest(self):
        webserver.add_job("security-fixture", webserver.create_event_queue(), KEY)
        try:
            record = webserver.get_running_job("security-fixture")
            self.assertNotIn(KEY, repr(record))
            self.assertNotIn("pmdb_api_key", record)
            self.assertEqual(len(webserver.search_running_jobs(KEY)), 1)
            self.assertEqual(webserver.search_running_jobs("different-key"), [])
        finally:
            webserver.remove_job("security-fixture")

    @unittest.skipUnless(sys.platform == "linux", "Linux process protections")
    def test_linux_dump_protection_in_isolated_process(self):
        code = """from process_security import harden_process
import ctypes,resource
harden_process()
assert resource.getrlimit(resource.RLIMIT_CORE) == (0,0)
assert ctypes.CDLL(None).prctl(3,0,0,0,0) == 0
"""
        subprocess.run([sys.executable, "-c", code], check=True, timeout=10)
