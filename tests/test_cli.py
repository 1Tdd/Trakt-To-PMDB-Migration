import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import requests
import user


def token(expired=False):
    return {
        "access_token": "test-only-trakt-access",
        "token_type": "bearer",
        "username": "test-user",
        "created_at": int(time.time()) - (7200 if expired else 10),
        "expires_in": 3600,
        "refresh_token": "test-only-refresh",
    }


class CliTests(unittest.TestCase):
    def setUp(self):
        self.previous_dir = Path.cwd()
        self.temp_dir = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2])
        os.chdir(self.temp_dir.name)
        self.addCleanup(self.restore_directory)
        self.output = io.StringIO()
        self.enter_context(contextlib.redirect_stdout(self.output))
        self.enter_context(patch.dict(os.environ, {
            "pmdb_api_key": "test-only-pmdb-key", "trakt_client": "test-only-client", "trakt_secret": "test-only-secret",
        }))
        self.refresh = self.enter_context(patch.object(user.requests, "post", side_effect=AssertionError("Unexpected network call")))
        self.authorize = self.enter_context(patch.object(user, "code_authorize_user", return_value=None))
        self.profile = self.enter_context(patch.object(user, "add_user_information", return_value=None))
        self.stages = {}
        for name in ("sync_watchlist", "sync_lists", "sync_show_watch_history", "sync_movie_watch_history", "sync_resume_points"):
            self.stages[name] = self.enter_context(patch.object(user, name, return_value=True))

    def enter_context(self, manager):
        value = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        return value

    def restore_directory(self):
        os.chdir(self.previous_dir)
        self.temp_dir.cleanup()

    def save_token(self, value):
        Path("token.json").write_text(json.dumps(value), encoding="utf-8")

    def test_cli_uses_current_context_and_resume_interfaces(self):
        data = token()
        self.save_token(data)
        with patch("builtins.input", return_value="y"):
            self.assertEqual(user.run_cli(), 0)
        ctx = self.stages["sync_watchlist"].call_args.args[0]
        self.assertEqual(ctx.pmdb_headers, {"Authorization": "Bearer test-only-pmdb-key"})
        self.assertEqual(ctx.trakt_headers["Authorization"], "bearer test-only-trakt-access")
        self.assertEqual(ctx.trakt_profile, {"username": "test-user"})
        self.assertIsNone(ctx.trakt_data)
        self.assertEqual(self.stages["sync_resume_points"].call_args_list[0].args, (ctx, "episodes"))
        self.assertEqual(self.stages["sync_resume_points"].call_args_list[1].args, (ctx, "movies"))
        self.authorize.assert_not_called()
        self.refresh.assert_not_called()

    def test_cli_runs_only_selected_stage(self):
        self.save_token(token())
        with patch("builtins.input", side_effect=["n", "n", "n", "n", "n", "y", "n"]):
            self.assertEqual(user.run_cli(), 0)
        for name, stage in self.stages.items():
            if name != "sync_resume_points":
                stage.assert_not_called()
        self.assertEqual(self.stages["sync_resume_points"].call_count, 1)
        self.assertEqual(self.stages["sync_resume_points"].call_args.args[1], "episodes")

    def test_failed_authentication_does_not_create_token_or_start_migration(self):
        self.assertEqual(user.run_cli(), 1)
        self.assertFalse(Path("token.json").exists())
        for stage in self.stages.values():
            stage.assert_not_called()

    def test_failed_authentication_preserves_existing_invalid_cache(self):
        Path("token.json").write_text("invalid-existing-cache", encoding="utf-8")
        self.assertEqual(user.run_cli(), 1)
        self.assertEqual(Path("token.json").read_text(encoding="utf-8"), "invalid-existing-cache")

    def test_incomplete_authentication_is_not_persisted(self):
        self.authorize.return_value = {"access_token": "test-only-incomplete"}
        self.assertEqual(user.run_cli(), 1)
        self.assertFalse(Path("token.json").exists())

    def test_missing_pmdb_key_stops_before_authorizing(self):
        with patch.dict(os.environ, {"pmdb_api_key": ""}):
            self.assertEqual(user.run_cli(), 1)
        self.authorize.assert_not_called()
        self.assertFalse(Path("token.json").exists())

    def test_valid_cache_reused_without_network(self):
        data = token()
        self.save_token(data)
        self.assertEqual(user.check_for_existing_token(), data)
        self.refresh.assert_not_called()

    def test_malformed_cache_rejected_without_network(self):
        for value in ({}, [], {**token(), "created_at": "invalid"}, {**token(), "expires_in": float("nan")}):
            with self.subTest(value_type=type(value).__name__):
                self.save_token(value)
                self.assertIsNone(user.check_for_existing_token())
        self.refresh.assert_not_called()

    def test_refresh_is_bounded_and_failure_does_not_log_provider_body(self):
        old = token(expired=True)
        self.save_token(old)
        self.refresh.side_effect = None
        self.refresh.return_value = Mock(status_code=401, text="test-only-secret test-only-trakt-access")
        self.assertIsNone(user.check_for_existing_token())
        self.assertEqual(self.refresh.call_args.kwargs["timeout"], user.TOKEN_REFRESH_TIMEOUT)
        self.assertFalse(self.refresh.call_args.kwargs["allow_redirects"])
        self.assertEqual(json.loads(Path("token.json").read_text()), old)
        self.assertNotIn("test-only-secret", self.output.getvalue())
        self.assertNotIn("test-only-trakt-access", self.output.getvalue())

    def test_refresh_timeout_is_safe_and_preserves_cache(self):
        old = token(expired=True)
        self.save_token(old)
        self.refresh.side_effect = requests.Timeout("test-only-secret")
        self.assertIsNone(user.check_for_existing_token())
        self.assertEqual(json.loads(Path("token.json").read_text()), old)
        self.assertNotIn("test-only-secret", self.output.getvalue())

    def test_refresh_profile_failure_preserves_cache(self):
        old = token(expired=True)
        self.save_token(old)
        self.refresh.side_effect = None
        response = Mock(status_code=200)
        response.json.return_value = {key: value for key, value in token().items() if key != "username"}
        self.refresh.return_value = response
        self.assertIsNone(user.check_for_existing_token())
        self.assertEqual(json.loads(Path("token.json").read_text()), old)

    def test_successful_refresh_is_saved_only_after_profile_validation(self):
        self.save_token(token(expired=True))
        updated = {**token(), "access_token": "test-only-new-access"}
        response = Mock(status_code=200)
        response.json.return_value = {key: value for key, value in updated.items() if key != "username"}
        self.refresh.side_effect = None
        self.refresh.return_value = response
        self.profile.return_value = updated
        with patch("builtins.input", return_value="y"):
            self.assertEqual(user.run_cli(), 0)
        self.assertEqual(json.loads(Path("token.json").read_text()), updated)
        self.authorize.assert_not_called()

    def test_cli_reports_failed_stage_with_nonzero_exit(self):
        self.save_token(token())
        self.stages["sync_watchlist"].return_value = False
        with patch("builtins.input", return_value="y"):
            self.assertEqual(user.run_cli(), 1)

    def test_cli_suppresses_sensitive_stage_exception_details(self):
        self.save_token(token())
        self.stages["sync_watchlist"].side_effect = requests.Timeout("test-only-pmdb-key")
        with patch("builtins.input", return_value="y"):
            self.assertEqual(user.run_cli(), 1)
        self.assertNotIn("test-only-pmdb-key", self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
