import asyncio
import contextlib
import io
import json
import os
import queue
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from unittest.mock import AsyncMock, Mock, patch

os.environ["domain"] = "https://migration.test"

from fastapi.testclient import TestClient
from fastapi import Response
import main
import webserver

KEY = "test-only-job-owner-credential"
HEADERS = {"Origin": "https://migration.test", "X-Requested-With": "TraktMigration"}
STAGE_FUNCTIONS = ("sync_watchlist", "sync_lists", "sync_show_watch_history", "sync_movie_watch_history", "sync_dropped_shows", "sync_resume_points")


class MigrationJobTests(unittest.TestCase):
    def setUp(self):
        webserver.auth_budget = webserver.RateBudget()
        webserver.running_jobs.clear()
        webserver.completed_jobs.clear()
        webserver.sessions.clear()
        webserver.shutdown_requested.clear()
        self.client = TestClient(webserver.app, base_url="https://migration.test")

    def tearDown(self):
        webserver.running_jobs.clear()
        webserver.completed_jobs.clear()
        webserver.sessions.clear()

    def login(self, key=KEY):
        with patch.object(webserver, "check_pmdb_token", return_value=True):
            response = self.client.post("/pmdb/auth", headers={**HEADERS, "Authorization": "Bearer " + key})
        self.assertEqual(response.status_code, 200)

    def run_job(self, options, overrides=None, job_id="finished-job"):
        events = queue.Queue()
        context = main.build_sync_context(KEY, events, {
            "user-profile": {"username": "fixture"}, "watched-playback": [],
            "watched-history": [], "lists-watchlist": [], "lists-lists": [], "hidden-dropped": [],
        })
        webserver.add_job(job_id, events, KEY)
        with contextlib.ExitStack() as stack:
            mocks = {}
            for name in STAGE_FUNCTIONS:
                behavior = (overrides or {}).get(name, {"return_value": True})
                mocks[name] = stack.enter_context(patch.object(webserver, name, **behavior))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            webserver.migrate_data(context, options, events, job_id)
        records = []
        while not events.empty():
            records.append(events.get_nowait())
        return records, mocks, context

    def test_false_stage_is_failed_and_other_selected_stages_continue(self):
        records, mocks, context = self.run_job(
            {"sync_watchlist_choice": True, "sync_show_resume_points_choice": True, "sync_movie_resume_points_choice": True},
            {"sync_watchlist": {"return_value": False}},
        )
        result = records[-1]
        self.assertEqual(result["type"], "complete")
        self.assertFalse(result["success"])
        self.assertEqual(result["failed_steps"], ["sync_watchlist_choice"])
        self.assertEqual([row["status"] for row in result["steps"]], ["failed", "skipped", "skipped", "skipped", "skipped", "completed", "completed"])
        self.assertEqual(mocks["sync_resume_points"].call_count, 2)
        self.assertIsNone(context.pmdb_headers)
        self.assertIsNone(context.trakt_data)
        self.assertEqual(webserver.running_jobs, [])

    def test_none_result_cannot_be_reported_as_success(self):
        records, _, _ = self.run_job({"sync_watchlist_choice": True}, {"sync_watchlist": {"return_value": None}})
        self.assertFalse(records[-1]["success"])

    def test_nonselected_stages_are_skipped_and_never_called(self):
        records, mocks, _ = self.run_job({"sync_movie_resume_points_choice": True})
        self.assertTrue(records[-1]["success"])
        for name, function in mocks.items():
            self.assertEqual(function.call_count, 1 if name == "sync_resume_points" else 0)
        self.assertEqual(records[-1]["steps"][-1]["status"], "completed")
        self.assertEqual([entry["progress"] for entry in records if entry["type"] == "progress"], [14, 29, 43, 57, 71, 86, 100])

    def test_network_timeout_is_reported_without_retry_or_secret_and_continues(self):
        records, mocks, _ = self.run_job(
            {"sync_watchlist_choice": True, "sync_lists_choice": True},
            {"sync_watchlist": {"side_effect": webserver.RequestException(KEY)}},
        )
        self.assertFalse(records[-1]["success"])
        self.assertEqual(mocks["sync_watchlist"].call_count, 1)
        self.assertEqual(mocks["sync_lists"].call_count, 1)
        self.assertNotIn(KEY, json.dumps(records))

    def test_unexpected_exception_marks_remaining_selected_stages_not_run(self):
        records, mocks, _ = self.run_job(
            {"sync_watchlist_choice": True, "sync_lists_choice": True, "sync_movie_resume_points_choice": True},
            {"sync_watchlist": {"side_effect": RuntimeError(KEY)}},
        )
        result = records[-1]
        self.assertEqual(result["type"], "critical")
        self.assertFalse(result["success"])
        self.assertEqual(result["failed_steps"], ["sync_watchlist_choice", "sync_lists_choice", "sync_movie_resume_points_choice"])
        self.assertEqual(result["steps"][1]["status"], "not-run")
        mocks["sync_lists"].assert_not_called()
        self.assertNotIn(KEY, json.dumps(result))

    def test_fast_job_completed_before_events_connect_can_be_read_repeatedly(self):
        records, _, _ = self.run_job({"sync_show_resume_points_choice": True})
        self.login()
        for _ in range(2):
            response = self.client.get("/migrate/finished-job/events")
            self.assertEqual(response.status_code, 200)
            self.assertIn('"success": true', response.text)
            self.assertEqual(response.text.count('"type": "complete"'), 1)
            self.assertIn("show-resume", response.text)
            self.assertNotIn(KEY, response.text)
        self.assertEqual(webserver.completed_jobs["finished-job"]["terminal_event"], records[-1])

    def test_completed_result_is_owned_and_retains_no_plaintext_credentials(self):
        self.run_job({"sync_watchlist_choice": True})
        self.assertNotIn(KEY, json.dumps(webserver.completed_jobs))
        self.assertNotIn("pmdb_api_key", webserver.completed_jobs["finished-job"])
        self.login("test-only-another-account-credential")
        self.assertEqual(self.client.get("/migrate/finished-job/events").status_code, 403)

    def test_completed_result_expires_and_cache_is_bounded(self):
        with patch.object(webserver.time, "monotonic", return_value=1000):
            for number in range(webserver.MAX_COMPLETED_JOBS + 1):
                job_id = f"bounded-{number}"
                webserver.add_job(job_id, queue.Queue(), KEY)
                webserver.finish_job(job_id, {"type": "complete", "success": True})
        self.assertEqual(len(webserver.completed_jobs), webserver.MAX_COMPLETED_JOBS)
        self.assertNotIn("bounded-0", webserver.completed_jobs)
        with patch.object(webserver.time, "monotonic", return_value=1000 + webserver.COMPLETED_JOB_TTL):
            self.assertIsNone(webserver.get_completed_job("bounded-1"))
        self.assertEqual(len(webserver.completed_jobs), 0)

    def test_two_streams_receive_same_events_without_consuming_each_other(self):
        events = webserver.create_event_queue()
        webserver.add_job("broadcast", events, KEY)
        for event in ({"type": "progress", "step": 1}, {"type": "complete", "success": True}):
            events.put_nowait(event)

        async def collect():
            async def read():
                request = Mock(headers={}, cookies={webserver.session_cookie_name: webserver.sessions.create(KEY)})
                request.is_disconnected = AsyncMock(return_value=False)
                stream = webserver.stream_sync_job("broadcast", KEY, request)
                return [chunk async for chunk in stream.body_iterator]
            return await asyncio.gather(read(), read())

        first, second = asyncio.run(collect())
        self.assertEqual(first, second)
        self.assertEqual(len(first), 2)
        self.assertIn("id: 1", first[0])
        self.assertIn('"type": "complete"', first[-1])

    def test_reconnect_resumes_after_last_event_id(self):
        events = webserver.create_event_queue()
        webserver.add_job("reconnect", events, KEY)
        events.put_nowait({"type": "progress"})
        events.put_nowait({"type": "complete"})

        async def read():
            request = Mock(headers={"last-event-id": "1"}, cookies={webserver.session_cookie_name: webserver.sessions.create(KEY)})
            request.is_disconnected = AsyncMock(return_value=False)
            stream = webserver.stream_sync_job("reconnect", KEY, request)
            return [chunk async for chunk in stream.body_iterator]

        chunks = asyncio.run(read())
        self.assertEqual(len(chunks), 1)
        self.assertIn("id: 2", chunks[0])

    def test_slow_clients_retain_recent_events_with_bounded_memory(self):
        events = webserver.create_event_queue()
        for index in range(600):
            events.put_nowait({"type": "progress", "step": index})
        self.assertEqual(events.qsize(), 500)
        history = events.read_after(0)
        self.assertEqual(len(history), 500)
        self.assertEqual(history[0][1]["step"], 100)
        self.assertEqual(history[-1][1]["step"], 599)
        webserver.enqueue_event(events, {"type": "complete"})
        self.assertEqual(events.read_after(600)[0][1]["type"], "complete")

    def test_logout_revokes_an_already_open_stream(self):
        events = webserver.create_event_queue()
        webserver.add_job("revoked", events, KEY)
        events.put_nowait({"type": "progress"})
        events.put_nowait({"type": "complete"})
        token = webserver.sessions.create(KEY)

        async def read():
            request = Mock(headers={}, cookies={webserver.session_cookie_name: token})
            request.is_disconnected = AsyncMock(return_value=False)
            stream = webserver.stream_sync_job("revoked", KEY, request).body_iterator
            first = await anext(stream)
            webserver.sessions.revoke(token)
            rest = [chunk async for chunk in stream]
            return first, rest

        first, rest = asyncio.run(read())
        self.assertIn('"type": "progress"', first)
        self.assertEqual(len(rest), 1)
        self.assertIn("session_expired", rest[0])
        self.assertIsNotNone(webserver.get_running_job("revoked"))

    def test_global_job_capacity_blocks_new_workers(self):
        for number in range(webserver.MAX_ACTIVE_JOBS):
            webserver.add_job(str(number), queue.Queue(), "other-key-" + str(number))
        self.login()
        with patch.object(webserver, "create_sync_job") as create:
            result = self.client.post("/migrate", headers=HEADERS,
                json={"trakt_data": {"user-profile": {"username": "fixture"}}})
        self.assertEqual(result.status_code, 503)
        create.assert_not_called()

    def test_concurrent_requests_for_one_owner_start_only_one_job(self):
        entered_startup = threading.Event()
        release_startup = threading.Event()
        second_requested = threading.Event()
        original_context = main.build_sync_context
        options = webserver.sync_options(trakt_data={"user-profile": {"username": "fixture"}})

        def delayed_context(*args):
            entered_startup.set()
            if not release_startup.wait(5):
                raise AssertionError("Test did not release migration startup")
            return original_context(*args)

        def submit(response, is_second=False):
            if is_second:
                second_requested.set()
            return webserver.request_data_migration(options, response, None)

        first_response, second_response = Response(), Response()
        worker = Mock()
        # No worker or API operation is started: keep the registered fixture job active.
        with patch.object(webserver, "session_key", return_value=KEY), \
                patch.object(webserver, "build_sync_context", side_effect=delayed_context) as build, \
                patch.object(webserver, "create_sync_job", side_effect=lambda ctx, choices, events: ("concurrent-fixture", events, worker)):
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(submit, first_response)
                self.assertTrue(entered_startup.wait(5))
                second = executor.submit(submit, second_response, True)
                try:
                    self.assertTrue(second_requested.wait(5))
                    with self.assertRaises(FutureTimeout):
                        second.result(timeout=0.05)
                finally:
                    release_startup.set()
                first_result, second_result = first.result(timeout=5), second.result(timeout=5)

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 409)
        self.assertEqual(first_result["job_id"], second_result["job_id"])
        self.assertEqual(len(webserver.running_jobs), 1)
        build.assert_called_once()
        worker.start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
