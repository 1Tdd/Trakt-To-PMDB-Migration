import queue
import unittest
from unittest.mock import patch
import test_migration_jobs as helpers
KEY, HEADERS = helpers.KEY, helpers.HEADERS
import job_control
import main
import webserver


class JobControlTests(unittest.TestCase):
    setUp = helpers.MigrationJobTests.setUp
    tearDown = helpers.MigrationJobTests.tearDown
    login = helpers.MigrationJobTests.login
    run_job = helpers.MigrationJobTests.run_job

    def test_finish_forgets_all_matching_keys_but_preserves_result_access(self):
        self.login()
        token = self.client.cookies.get(webserver.session_cookie_name)
        other_session = webserver.sessions.create(KEY)
        unrelated = webserver.sessions.create('unrelated')
        self.run_job({'sync_watchlist_choice': True})
        self.assertIsNone(webserver.sessions.get(token))
        self.assertIsNone(webserver.sessions.get(other_session))
        self.assertEqual(webserver.sessions.get(unrelated), 'unrelated')
        self.assertEqual(self.client.get('/migrate/finished-job/events').status_code, 200)
        self.assertEqual(self.client.post('/migrate', headers=HEADERS, json={'trakt_data': {}}).status_code, 401)
        status = self.client.get('/auth/status').json()
        self.assertFalse(status['pmdb'])
        self.assertTrue(status['can_view_results'])
        self.assertGreater(status['expires_in'], 0)

    def test_stop_cannot_cancel_other_owner_and_retains_no_session_key(self):
        self.login()
        token = self.client.cookies.get(webserver.session_cookie_name)
        webserver.add_job('mine', queue.Queue(), KEY)
        webserver.add_job('other', queue.Queue(), 'someone-else')
        self.assertEqual(self.client.post('/pmdb/stop-and-disconnect').status_code, 403)
        response = self.client.post('/pmdb/stop-and-disconnect', headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['stopping_jobs'], 1)
        self.assertTrue(webserver.get_running_job('mine')['control'].cancelled.is_set())
        self.assertFalse(webserver.get_running_job('other')['control'].cancelled.is_set())
        self.assertIsNone(webserver.sessions.get(token))
        self.assertIsNotNone(webserver.sessions.owner(token))

    def test_logout_does_not_stop_job(self):
        self.login()
        webserver.add_job('mine', queue.Queue(), KEY)
        response = self.client.post('/pmdb/logout', headers=HEADERS)
        self.assertEqual(response.json()['running_jobs'], 1)
        self.assertFalse(webserver.get_running_job('mine')['control'].cancelled.is_set())
        self.assertEqual(self.client.post('/pmdb/stop-and-disconnect', headers=HEADERS).status_code, 401)

    def test_cancel_and_deadline_never_start_next_stage_or_report_success(self):
        for reason in ('cancel', 'deadline'):
            def stop(ctx):
                control = webserver.get_running_job('finished-job')['control']
                if reason == 'cancel':
                    control.cancelled.set()
                else:
                    control.deadline = 0
                return True
            self.login()
            token = self.client.cookies.get(webserver.session_cookie_name)
            records, mocks, context = self.run_job(
                {'sync_watchlist_choice': True, 'sync_lists_choice': True},
                {'sync_watchlist': {'side_effect': stop}})
            terminal = records[-1]
            self.assertEqual(terminal['type'], 'critical')
            self.assertFalse(terminal['success'])
            self.assertEqual(terminal['steps'][0]['status'], 'completed')
            self.assertIn('stopped', terminal['message'])
            mocks['sync_lists'].assert_not_called()
            self.assertIsNone(context.pmdb_headers)
            self.assertIsNone(webserver.sessions.get(token))
            job_control.checkpoint()  # Worker binding must have been cleared.


class TransportControlTests(unittest.TestCase):
    def test_cancel_or_expired_job_sends_no_request(self):
        for control in (job_control.JobControl(), job_control.JobControl(max_seconds=0)):
            if control.deadline > job_control.time.monotonic():
                control.cancelled.set()
            job_control.bind(control)
            try:
                with patch('requests.Session.request') as request:
                    with self.assertRaises(job_control.MigrationStopped):
                        main.session.get('https://example.test')
                    request.assert_not_called()
            finally:
                job_control.bind(None)
