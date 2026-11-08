import contextlib
import io
import os
import queue
import unittest
from unittest.mock import Mock, patch

os.environ["domain"] = "https://migration.test"

from fastapi.testclient import TestClient
from auth_sessions import SessionStore
import main
import start
import webserver

FAKE_KEY = "test-only-pmdb-credential-0123456789"
HEADERS = {"Origin": "https://migration.test", "X-Requested-With": "TraktMigration"}


class SecurityTests(unittest.TestCase):
    def setUp(self):
        webserver.auth_budget = webserver.RateBudget()
        webserver.sessions.clear()
        webserver.running_jobs.clear()
        self.client = TestClient(webserver.app, base_url="https://migration.test")

    def login(self, key=FAKE_KEY):
        with patch.object(webserver, "check_pmdb_token", return_value=True):
            return self.client.post("/pmdb/auth", headers={**HEADERS, "Authorization": "Bearer " + key})

    def test_auth_rate_limit_stops_provider_requests(self):
        webserver.auth_budget = webserver.RateBudget(limit=1)
        self.assertEqual(self.login().status_code, 200)
        with patch.object(webserver, "check_pmdb_token") as check:
            result = self.client.post("/pmdb/auth", headers={**HEADERS, "Authorization": "Bearer " + FAKE_KEY})
        self.assertEqual(result.status_code, 429)
        self.assertEqual(result.headers["retry-after"], "60")
        check.assert_not_called()

    def test_cookie_is_opaque_secure_and_does_not_contain_credentials(self):
        response = self.login()
        self.assertEqual(response.status_code, 200)
        cookies = "; ".join(response.headers.get_list("set-cookie"))
        for flag in ("__Host-pmdb_session=", "HttpOnly", "Secure", "SameSite=strict", "Max-Age=3600", "Path=/"):
            self.assertIn(flag, cookies)
        self.assertNotIn(FAKE_KEY, cookies + response.text)
        self.assertNotIn(FAKE_KEY[:15], cookies + response.text)
        token = self.client.cookies.get(webserver.session_cookie_name)
        self.assertGreaterEqual(len(token), 40)
        self.assertEqual(webserver.sessions.get(token), FAKE_KEY)

    def test_status_returns_no_key_or_key_prefix(self):
        self.login()
        response = self.client.get("/auth/status")
        self.assertTrue(response.json()["pmdb"])
        self.assertNotIn(FAKE_KEY[:15], response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_old_encrypted_credentials_are_rejected_and_cookie_removed(self):
        self.client.cookies.set("pmdb_auth", "old-encrypted-credential")
        response = self.client.get("/auth/status")
        self.assertFalse(response.json()["pmdb"])
        self.assertIn("Max-Age=0", response.headers["set-cookie"])
        response = self.client.post("/migrate", headers=HEADERS, json={"trakt_data": {"user-profile": {"username": "test"}}})
        self.assertEqual(response.status_code, 401)

    def test_logout_invalidates_cookie_replay(self):
        self.login()
        token = self.client.cookies.get(webserver.session_cookie_name)
        self.assertEqual(self.client.post("/pmdb/logout", headers=HEADERS).status_code, 200)
        self.assertIsNone(webserver.sessions.get(token))
        self.client.cookies.set(webserver.session_cookie_name, token)
        self.assertFalse(self.client.get("/auth/status").json()["pmdb"])

    def test_new_login_revokes_previous_session(self):
        self.login()
        old = self.client.cookies.get(webserver.session_cookie_name)
        self.login()
        self.assertIsNone(webserver.sessions.get(old))

    def test_expired_sessions_are_purged_and_cannot_authenticate(self):
        now = [0]
        store = SessionStore(ttl=10, clock=lambda: now[0])
        token = store.create(FAKE_KEY)
        now[0] = 10
        store.purge()
        self.assertIsNone(store.get(token))
        self.assertEqual(len(store._entries), 0)
        with patch.object(webserver, "sessions", store):
            self.client.cookies.set(webserver.session_cookie_name, token)
            self.assertFalse(self.client.get("/auth/status").json()["pmdb"])
            self.assertEqual(self.client.get("/migrate/unknown/events").status_code, 401)

    def test_restart_invalidates_sessions(self):
        self.login()
        webserver.sessions.clear()
        self.assertFalse(self.client.get("/auth/status").json()["pmdb"])

    def test_cross_origin_and_missing_custom_header_requests_are_blocked(self):
        for headers in ({"Origin": "https://attacker.test", "X-Requested-With": "TraktMigration"},
                        {"Origin": "https://migration.test"},
                        {**HEADERS, "Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers), patch.object(webserver, "check_pmdb_token") as check:
                response = self.client.post("/pmdb/auth", headers={**headers, "Authorization": "Bearer " + FAKE_KEY})
                self.assertEqual(response.status_code, 403)
                check.assert_not_called()
                self.assertEqual(self.client.post("/migrate", headers=headers, json={"trakt_data": {}}).status_code, 403)

    def test_other_users_cannot_read_job_events(self):
        events = queue.Queue()
        events.put({"type": "complete", "message": "Done"})
        webserver.add_job("test-job", events, FAKE_KEY)
        self.login("test-only-different-account-key")
        self.assertEqual(self.client.get("/migrate/test-job/events").status_code, 403)
        self.login()
        response = self.client.get("/migrate/test-job/events")
        self.assertEqual(response.status_code, 200)
        self.assertIn('"type": "complete"', response.text)
        self.assertNotIn(FAKE_KEY, response.text)

    def test_pmdb_error_body_is_not_logged(self):
        output = io.StringIO()
        response = Mock(status_code=401, text="provider echoed " + FAKE_KEY)
        with patch("main.requests.request", return_value=response), contextlib.redirect_stdout(output):
            self.assertFalse(main.check_pmdb_token(FAKE_KEY))
        self.assertNotIn(FAKE_KEY, output.getvalue())

    def test_credentials_are_redacted_in_job_logs(self):
        events = queue.Queue()
        ctx = main.build_sync_context(FAKE_KEY, events, {"user-profile": {"username": "test"}})
        output = io.StringIO()
        with patch.dict(os.environ, {"log_to_console": "true"}), contextlib.redirect_stdout(output):
            main.log("Echoed credential: " + FAKE_KEY, ctx)
        self.assertNotIn(FAKE_KEY, output.getvalue())
        self.assertNotIn(FAKE_KEY, str(events.get_nowait()))

    def test_exception_credentials_are_not_sent_to_logs_or_events(self):
        events = queue.Queue()
        ctx = main.build_sync_context(FAKE_KEY, events, {"user-profile": {"username": "test"}})
        output = io.StringIO()
        with patch.object(webserver, "sync_watchlist", side_effect=RuntimeError(FAKE_KEY)), contextlib.redirect_stdout(output):
            webserver.migrate_data(ctx, {"sync_watchlist_choice": True}, events, "test")
        self.assertNotIn(FAKE_KEY, output.getvalue())
        event = events.get_nowait()
        self.assertEqual(event["type"], "critical")
        self.assertNotIn(FAKE_KEY, str(event))
        self.assertIsNone(ctx.pmdb_headers)

    def test_security_headers_and_external_scripts(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        self.assertNotIn("script-src 'self' 'unsafe-inline'", response.headers["content-security-policy"])
        self.assertNotIn('<script language=', response.text)
        self.assertIn('type="password"', response.text)
        self.assertEqual(self.client.get("/app.js").status_code, 200)

    def test_failed_job_start_does_not_leave_credentials_in_job_registry(self):
        self.login()
        thread = Mock()
        thread.start.side_effect = RuntimeError(FAKE_KEY)
        with patch.object(webserver, "create_sync_job", return_value=("failed-job", queue.Queue(), thread)), contextlib.redirect_stdout(io.StringIO()) as output:
            response = self.client.post("/migrate", headers=HEADERS, json={"trakt_data": {"user-profile": {"username": "test"}}})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(webserver.running_jobs, [])
        self.assertNotIn(FAKE_KEY, response.text + output.getvalue())

    def test_boot_uses_assigned_port_and_one_worker(self):
        with patch.dict(os.environ, {"domain": "https://migration.test", "SERVER_PORT": "43123"}), patch.object(start.uvicorn, "run") as run:
            start.main()
        self.assertEqual(run.call_args.kwargs["port"], 43123)
        self.assertEqual(run.call_args.kwargs["host"], "0.0.0.0")
        self.assertEqual(run.call_args.kwargs["workers"], 1)

    def test_boot_rejects_public_http_and_invalid_ports(self):
        for domain, port in (("http://migration.test", "8000"), ("https://migration.test", "0")):
            with self.subTest(domain=domain, port=port), patch.dict(os.environ, {"domain": domain, "SERVER_PORT": port}), self.assertRaises(SystemExit):
                start.main()


if __name__ == "__main__":
    unittest.main()
