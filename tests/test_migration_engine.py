import datetime
import json
import queue
import unittest
from unittest.mock import Mock, patch

import requests
import main


def response(status=200, data=None):
    result = Mock(status_code=status)
    result.json.return_value = {} if data is None else data
    return result


def context(data=None):
    export = {"user-profile": {"username": "synthetic-test-user"}}
    if data is not None:
        export.update(data)
    return main.build_sync_context("test-only-pmdb-key", queue.Queue(), export)


def movie_playback(index=1, progress=25):
    return {"type": "movie", "progress": progress, "paused_at": "2026-01-02T12:00:00Z",
            "movie": {"ids": {"tmdb": index, "trakt": index}}}


def episode_playback(index=1, season=1):
    return {"type": "episode", "progress": 50, "paused_at": "2026-01-01T12:00:00",
            "show": {"ids": {"tmdb": 100, "trakt": 200}},
            "episode": {"season": season, "number": index}}


class MigrationEngineTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict("os.environ", {"domain": "https://migration.test", "log_to_console": "false"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.network_patch = patch.object(main, "session")
        self.session = self.network_patch.start()
        self.addCleanup(self.network_patch.stop)

    def test_exported_resume_points_import_both_media_types_without_oauth(self):
        data = [movie_playback(1), episode_playback(1, season=0), movie_playback(2), episode_playback(2)]
        ctx = context({"watched-playback": data})
        self.session.post.return_value = response(data={"summary": {"saved": 2}})
        self.assertTrue(main.sync_resume_points(ctx, "episodes"))
        self.assertTrue(main.sync_resume_points(ctx, "movies"))
        self.session.get.assert_not_called()
        calls = self.session.post.call_args_list
        self.assertEqual(len(calls), 2)
        episode_items = calls[0].kwargs["json"]["items"]
        self.assertEqual([item["media_type"] for item in episode_items], ["tv", "tv"])
        self.assertEqual(episode_items[0]["season"], 0)
        self.assertEqual(episode_items[0]["position_ms"], 500000)
        self.assertEqual([item["media_type"] for item in calls[1].kwargs["json"]["items"]], ["movie", "movie"])

    def test_empty_exported_playback_never_falls_back_to_trakt(self):
        for headers in (None, {"Authorization": "test-only-trakt-token"}):
            with self.subTest(oauth=bool(headers)):
                ctx = context({"watched-playback": []})
                ctx.trakt_headers = headers
                self.assertTrue(main.sync_resume_points(ctx, "movies"))
                self.assertTrue(main.sync_resume_points(ctx, "episodes"))
        self.session.get.assert_not_called()
        self.session.post.assert_not_called()

    def test_no_matching_playback_type_is_successful_without_requests(self):
        self.assertTrue(main.sync_resume_points(context({"watched-playback": [episode_playback()]}), "movies"))
        self.session.get.assert_not_called()
        self.session.post.assert_not_called()

    def test_missing_playback_without_oauth_fails_explicitly(self):
        self.assertFalse(main.sync_resume_points(context(), "movies"))
        self.session.get.assert_not_called()
        self.session.post.assert_not_called()

    def test_resume_points_keep_cli_oauth_fallback(self):
        ctx = context()
        ctx.trakt_headers = {"Authorization": "test-only-trakt-token"}
        self.session.get.return_value = response(data=[movie_playback()])
        self.session.post.return_value = response(data={"summary": {"saved": 1}})
        self.assertTrue(main.sync_resume_points(ctx, "movies"))
        self.assertEqual(self.session.get.call_args.args[0], main.trakt_api_url + "/sync/playback/movies")
        self.session.post.assert_called_once()

    def test_resume_batching_and_failure_are_reported(self):
        ctx = context({"watched-playback": [movie_playback(index) for index in range(1, 112)]})
        self.session.post.side_effect = lambda *args, **kwargs: response(data={"summary": {"saved": len(kwargs["json"]["items"])}})
        self.assertTrue(main.sync_resume_points(ctx, "movies"))
        self.assertEqual([len(call.kwargs["json"]["items"]) for call in self.session.post.call_args_list], [50, 50, 11])
        self.session.post.side_effect = None
        self.session.post.return_value = response(status=503)
        self.assertFalse(main.sync_resume_points(context({"watched-playback": [movie_playback()]}), "movies"))

    def test_resume_partial_api_summary_is_not_success(self):
        self.session.post.return_value = response(data={"summary": {"saved": 1}, "results": [{"action": "rejected"}]})
        self.assertFalse(main.submit_resume_point_batch_to_pmdb(context(), [movie_playback(1), movie_playback(2)]))

    def test_resume_policy_outcomes_succeed_without_changing_progress(self):
        items = [movie_playback(1, 1.58), movie_playback(2, 2), movie_playback(3, 80)]
        self.session.post.return_value = response(data={
            "results": [{"action": action} for action in ("ignored", "saved", "completed")],
            "summary": {"saved": 1, "ignored": 1, "completed": 1, "errors": 0}})
        ctx = context()
        self.assertTrue(main.submit_resume_point_batch_to_pmdb(ctx, items))
        payload = self.session.post.call_args.kwargs["json"]["items"]
        self.assertEqual([item["position_ms"] for item in payload], [15800, 20000, 800000])
        self.assertIn("1 saved, 1 ignored", json.dumps(list(ctx.event_queue.queue)))

    def test_single_resume_accepts_expected_policy_outcomes_only(self):
        for action in ("saved", "ignored", "completed", "rejected", None):
            with self.subTest(action=action):
                self.session.post.return_value = response(data={"action": action})
                self.assertEqual(main.submit_resume_point_to_pmdb(context(), movie_playback()),
                                 action in {"saved", "ignored", "completed"})

    def test_resume_incomplete_or_error_responses_never_pass(self):
        cases = [None, {}, {"results": []}, {"results": [None]},
                 {"results": [{"action": "saved"}], "summary": {"errors": 1}},
                 {"summary": {"ignored": -1, "saved": 2}},
                 {"summary": {"saved": True}}, {"summary": {"saved": "1"}},
                 {"summary": {"ignored": 1, "errors": 1}},
                 {"results": [{"action": []}]}, {"results": [{"action": {}}]},
                 {"results": [{"action": "saved"}], "summary": {"saved": 0}},
                 {"results": [{"action": "saved"}], "summary": None}]
        for data in cases:
            with self.subTest(data=data):
                self.assertFalse(main.report_resume_outcomes(context(), data, 1))

    def test_resume_summary_only_counts_all_expected_outcomes(self):
        self.assertTrue(main.report_resume_outcomes(context(), {
            "summary": {"saved": 1, "ignored": 1, "completed": 1, "errors": 0}}, 3))

    def test_empty_resume_batch_does_not_write(self):
        self.assertTrue(main.submit_resume_point_batch_to_pmdb(context(), []))
        self.session.post.assert_not_called()

    def test_special_episode_season_zero_is_preserved(self):
        self.session.post.return_value = response(data={"success": True})
        self.assertTrue(main.submit_watched_timestamp_to_pmdb(context(), 100, "tv", "2026-01-01T12:00:00Z", season=0, episode=1))
        body = self.session.post.call_args.kwargs["json"]
        self.assertEqual(body["season"], 0)
        self.assertEqual(body["episode"], 1)

    def test_export_history_preserves_special_episode_and_reports_progress(self):
        ctx = context()
        ctx.migration_step = 3
        ctx.migration_stage = "show_history"
        self.session.post.return_value = response(data={"success": True})
        item = {**episode_playback(1, season=0), "watched_at": "2026-01-01T12:00:00Z"}
        self.assertTrue(main.submit_exported_history_to_pmdb(ctx, "episode", [item]))
        self.assertEqual(self.session.post.call_args.kwargs["json"]["season"], 0)
        progress = [event for event in list(ctx.event_queue.queue) if event["type"] == "item_progress"]
        self.assertEqual(progress[-1]["completed"], 1)
        self.assertEqual(progress[-1]["total"], 1)
        self.assertEqual(progress[-1]["step"], 3)
        self.assertEqual(progress[-1]["stage"], "show_history")

    def test_invalid_episode_metadata_is_not_written(self):
        item = {"type": "episode", "show": {"ids": {"tmdb": 10}}, "episode": {"number": 1}}
        self.assertFalse(main.submit_exported_history_to_pmdb(context(), "episode", [item]))
        self.session.post.assert_not_called()

    def test_nested_movie_mapping_uses_media_ids_and_type(self):
        self.session.get.return_value = response(data={"results": [{"tmdb_id": 456}]})
        self.session.post.return_value = response(data={"success": True})
        item = {"type": "movie", "movie": {"ids": {"trakt": 123}}}
        self.assertTrue(main.add_to_pmdb_list(context(), "synthetic-list", item))
        self.assertNotIn("json", self.session.get.call_args.kwargs)
        self.assertEqual(self.session.get.call_args.kwargs["params"], {"id_type": "trakt", "id_value": 123, "media_type": "movie"})
        self.assertEqual(self.session.post.call_args.kwargs["json"]["tmdb_id"], 456)

    def test_unresolved_mappings_fail_without_writes_for_all_import_paths(self):
        ctx = context()
        movie = {"type": "movie", "movie": {"ids": {"trakt": 123}}}
        show = {"show": {"ids": {"trakt": 456}}, "seasons": []}
        cases = [lambda: main.add_to_pmdb_list(ctx, "synthetic-list", movie),
                 lambda: main.submit_history_movie_to_pmdb(ctx, movie),
                 lambda: main.submit_history_show_to_pmdb(ctx, show),
                 lambda: main.submit_exported_history_to_pmdb(ctx, "movie", [movie]),
                 lambda: main.submit_dropped_shows_to_pmdb(ctx, [show])]
        for index, run in enumerate(cases):
            with self.subTest(path=index):
                self.session.reset_mock()
                self.session.get.return_value = response(data={"results": []})
                self.assertFalse(run())
                self.session.post.assert_not_called()

    def test_empty_watchlist_is_success_without_creating_a_list(self):
        self.assertTrue(main.sync_watchlist(context({"lists-watchlist": []})))
        self.session.get.assert_not_called()
        self.session.post.assert_not_called()

    def test_missing_watchlist_is_failure_not_empty_success(self):
        self.assertFalse(main.sync_watchlist(context()))
        self.session.get.assert_not_called()
        self.session.post.assert_not_called()

    def test_watchlist_returns_failure_if_any_item_fails(self):
        self.session.get.return_value = response(data={"items": [{"type": "watchlist", "id": "synthetic-list"}]})
        self.session.post.side_effect = [response(data={"success": True}), response(status=503)]
        ctx = context({"lists-watchlist": [{"type": "movie", "movie": {"ids": {"tmdb": index}}} for index in (1, 2)]})
        self.assertFalse(main.sync_watchlist(ctx))
        self.assertEqual(self.session.post.call_count, 2)

    def test_watchlist_lookup_failure_does_not_write_items_to_none_id(self):
        self.session.get.return_value = response(status=503)
        ctx = context({"lists-watchlist": [{"type": "movie", "movie": {"ids": {"tmdb": 1}}}]})
        self.assertFalse(main.sync_watchlist(ctx))
        self.session.post.assert_not_called()

    def test_empty_custom_list_items_do_not_fetch_trakt(self):
        self.session.post.return_value = response(data={"success": True, "item": {"id": "synthetic-list"}})
        ctx = context({"lists-lists": [{"name": "Synthetic list", "items": []}]})
        self.assertTrue(main.sync_lists(ctx))
        self.session.get.assert_not_called()
        self.session.post.assert_called_once()

    def test_missing_custom_list_items_fail_before_creating_list(self):
        self.assertFalse(main.sync_lists(context({"lists-lists": [{"name": "Synthetic list"}]})))
        self.session.get.assert_not_called()
        self.session.post.assert_not_called()

    def test_empty_dropped_shows_are_success_missing_data_is_failure(self):
        self.assertFalse(main.sync_dropped_shows(context({"hidden-calendar": [{"show": {"ids": {"tmdb": 1}}}]})))
        self.assertTrue(main.sync_dropped_shows(context({"hidden-dropped": []})))
        self.assertFalse(main.sync_dropped_shows(context()))
        self.session.get.assert_not_called()
        self.session.post.assert_not_called()

    def test_missing_history_does_not_call_unauthenticated_trakt(self):
        self.assertFalse(main.sync_movie_watch_history(context()))
        self.assertFalse(main.sync_show_watch_history(context()))
        self.session.get.assert_not_called()
        self.session.request.assert_not_called()

    def test_date_sorting_accepts_missing_naive_aware_and_malformed_values(self):
        values = [None, "2026-01-01T12:00:00", "2026-01-01T13:00:00+01:00", "not-a-date", 123]
        dates = sorted(main.parse_listed_at(value) for value in values)
        self.assertTrue(all(value.tzinfo == datetime.timezone.utc for value in dates))
        self.assertEqual(main.parse_listed_at(values[1]), main.parse_listed_at(values[2]))

    def test_item_progress_is_bounded_and_contains_no_credentials(self):
        ctx = context()
        for completed in range(1001):
            main.report_item_progress(ctx, completed, 1000, "Importing watch history")
        events = list(ctx.event_queue.queue)
        self.assertLessEqual(len(events), 102)
        self.assertEqual(events[-1]["completed"], 1000)
        self.assertNotIn("test-only-pmdb-key", json.dumps(events))

    def test_all_shared_session_requests_have_finite_default_timeouts(self):
        with patch.object(requests.Session, "request", return_value=response()) as request:
            client = main.BoundedSession()
            client.get("https://example.test/read")
            client.post("https://example.test/write", json={})
            self.assertTrue(all(call.kwargs["timeout"] == main.REQUEST_TIMEOUT for call in request.call_args_list))
            self.assertEqual(client.adapters["https://"].max_retries.total, 0)

    def test_timed_out_write_is_not_automatically_retried(self):
        with patch.object(requests.Session, "request", side_effect=requests.Timeout("synthetic-timeout")) as request:
            with self.assertRaises(requests.Timeout):
                main.BoundedSession().post("https://example.test/write", json={})
            request.assert_called_once()


if __name__ == "__main__":
    unittest.main()
