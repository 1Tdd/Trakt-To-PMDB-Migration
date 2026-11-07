import asyncio
import unittest
from unittest.mock import patch
from resource_limits import RequestLimits, RateBudget, capacity_for, MIB

class ResourceLimitTests(unittest.TestCase):
    def test_capacity_scales_with_memory_and_has_upper_bounds(self):
        self.assertEqual(capacity_for(512 * MIB), (64 * MIB, 8))
        self.assertEqual(capacity_for(1024 * MIB), (128 * MIB, 16))
        self.assertEqual(capacity_for(16384 * MIB), (256 * MIB, 32))

    def test_memory_pressure_rejects_declared_and_chunked_requests_before_parsing(self):
        async def run(headers, body):
            called, output = [], []
            async def app(scope, receive, send):
                called.append(True)
            async def receive():
                return {'type': 'http.request', 'body': body}
            async def send(message):
                output.append(message)
            middleware = RequestLimits(app)
            with patch('resource_limits.container_memory', return_value=(512 * MIB, 448 * MIB)):
                await middleware({'type': 'http', 'method': 'POST', 'headers': headers}, receive, send)
            self.assertEqual(output[0]['status'], 503)
            self.assertFalse(called)
            self.assertEqual(middleware.reserved, 0)
            self.assertEqual(middleware.active, 0)
        asyncio.run(run([(b'content-length', b'1')], b'x'))
        asyncio.run(run([], b'x'))

    def test_concurrent_uploads_share_memory_reservations(self):
        async def run():
            entered, release = asyncio.Event(), asyncio.Event()
            async def app(scope, receive, send):
                entered.set()
                await release.wait()
            async def receive():
                return {'type': 'http.request', 'body': b'x'}
            messages = []
            async def send(message):
                messages.append(message)
            middleware = RequestLimits(app)
            scope = {'type': 'http', 'method': 'POST', 'headers': [(b'content-length', str(25*MIB).encode())]}
            with patch('resource_limits.container_memory', return_value=(512*MIB, 64*MIB)):
                first = asyncio.create_task(middleware(scope, receive, send))
                await entered.wait()
                await middleware(scope, receive, send)
                self.assertEqual(messages[0]['status'], 503)
                release.set()
                await first
            self.assertEqual(middleware.reserved, 0)
        asyncio.run(run())

    def test_rate_budget_recovers_after_window(self):
        now = [100]
        budget = RateBudget(limit=2, clock=lambda: now[0])
        self.assertTrue(budget.allow())
        self.assertTrue(budget.allow())
        self.assertFalse(budget.allow())
        now[0] += 60
        self.assertTrue(budget.allow())

    def test_chunked_and_declared_oversize_bodies_do_not_reach_app(self):
        async def run(headers, bodies):
            called, output = [], []
            async def app(scope, receive, send):
                called.append(True)
            async def receive():
                return bodies.pop(0)
            async def send(message):
                output.append(message)
            middleware = RequestLimits(app, max_bytes=4)
            await middleware({'type': 'http', 'method': 'POST', 'headers': headers}, receive, send)
            self.assertFalse(called)
            self.assertEqual(output[0]['status'], 413)
            self.assertEqual(middleware.active, 0)
        asyncio.run(run([(b'content-length', b'5')], []))
        asyncio.run(run([], [{'type': 'http.request', 'body': b'123', 'more_body': True},
                            {'type': 'http.request', 'body': b'45'}]))

    def test_valid_body_replays_and_budget_releases_on_app_error(self):
        async def run():
            async def app(scope, receive, send):
                self.assertEqual((await receive())['body'], b'1234')
                raise RuntimeError('fixture')
            async def receive():
                return {'type': 'http.request', 'body': b'1234'}
            middleware = RequestLimits(app, max_bytes=4)
            with self.assertRaises(RuntimeError):
                await middleware({'type': 'http', 'method': 'POST'}, receive, None)
            self.assertEqual(middleware.active, 0)
        asyncio.run(run())

    def test_simultaneous_uploads_are_bounded(self):
        async def run():
            entered, release = asyncio.Event(), asyncio.Event()
            async def app(scope, receive, send):
                entered.set()
                await release.wait()
            async def receive():
                return {'type': 'http.request', 'body': b''}
            messages = []
            async def send(message):
                messages.append(message)
            middleware = RequestLimits(app, max_requests=1)
            scope = {'type': 'http', 'method': 'POST'}
            first = asyncio.create_task(middleware(scope, receive, send))
            await entered.wait()
            await middleware(scope, receive, send)
            self.assertEqual(messages[0]['status'], 503)
            release.set()
            await first
            self.assertEqual(middleware.active, 0)
        asyncio.run(run())
