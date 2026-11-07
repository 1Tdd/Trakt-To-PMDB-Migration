"""Small process-wide budgets; do not trust client-supplied proxy/IP headers."""
import asyncio
import threading
import time
import os
from pathlib import Path
from starlette.responses import JSONResponse

MIB = 1024 * 1024

def container_memory():
    """Read the enforced Linux container budget, with a conservative fallback."""
    for limit_path, used_path in (
        ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory.current"),
        ("/sys/fs/cgroup/memory/memory.limit_in_bytes", "/sys/fs/cgroup/memory/memory.usage_in_bytes"),
    ):
        try:
            limit = int(Path(limit_path).read_text().strip())
            used = int(Path(used_path).read_text().strip())
            if 0 < limit < 1 << 50 and used >= 0:
                return limit, used
        except (OSError, ValueError):
            pass
    try:
        used = int(Path('/proc/self/statm').read_text().split()[1]) * os.sysconf('SC_PAGE_SIZE')
        return 512 * MIB, used
    except (OSError, ValueError, AttributeError, IndexError):
        return None

def capacity_for(memory_bytes):
    memory_mib = memory_bytes // MIB
    return (max(16, min(256, memory_mib // 8)) * MIB,
            max(1, min(32, memory_mib // 64)))

_memory = container_memory()
MAX_BODY_BYTES, MAX_ACTIVE_JOBS = capacity_for(_memory[0] if _memory else 512 * MIB)
# JSON expands into Python objects. Reserve 10x serialized bytes plus headroom.
JSON_MEMORY_FACTOR = 10
MEMORY_HEADROOM = 64 * MIB

class RateBudget:
    def __init__(self, limit=20, period=60, clock=time.monotonic):
        self.limit, self.period, self.clock = limit, period, clock
        self.started, self.used = clock(), 0
        self.lock = threading.Lock()

    def allow(self):
        with self.lock:
            now = self.clock()
            if now - self.started >= self.period:
                self.started, self.used = now, 0
            if self.used >= self.limit:
                return False
            self.used += 1
            return True

class RequestLimits:
    def __init__(self, app, max_bytes=MAX_BODY_BYTES, max_requests=2):
        self.app, self.max_bytes, self.max_requests = app, max_bytes, max_requests
        self.active = 0
        self.reserved = 0
        self.reservation_lock = threading.Lock()

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] not in {'POST', 'PUT', 'PATCH'}:
            return await self.app(scope, receive, send)
        async def reject(code, detail):
            await JSONResponse({'detail': detail}, status_code=code,
                               headers={'Cache-Control': 'no-store'})(scope, receive, send)
        if self.active >= self.max_requests:
            return await reject(503, 'Server busy; retry later')
        def reserve(amount):
            with self.reservation_lock:
                memory = container_memory()
                if memory and memory[1] + (self.reserved + amount) * JSON_MEMORY_FACTOR + MEMORY_HEADROOM > memory[0]:
                    return False
                self.reserved += amount
                return True
        reserved = 0
        # Read at most the budget before FastAPI allocates the parsed JSON tree.
        self.active += 1
        try:
            headers = dict(scope.get('headers', []))
            try:
                declared = int(headers.get(b'content-length', b'0'))
                if declared < 0:
                    raise ValueError
                if declared > self.max_bytes:
                    return await reject(413, f'Request exceeds the {self.max_bytes // MIB} MiB limit')
            except ValueError:
                return await reject(400, 'Invalid content length')
            if not reserve(declared):
                return await reject(503, 'Insufficient memory for this export; retry later or select fewer datasets')
            reserved = declared
            chunks, size = [], 0
            deadline = time.monotonic() + 120
            while True:
                try:
                    message = await asyncio.wait_for(receive(), max(0, deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    return await reject(408, 'Request upload timed out')
                if message['type'] == 'http.disconnect':
                    return
                size += len(message.get('body', b''))
                if size > self.max_bytes:
                    return await reject(413, f'Request exceeds the {self.max_bytes // MIB} MiB limit')
                extra = max(0, size - reserved)
                if extra and not reserve(extra):
                    return await reject(503, 'Insufficient memory for this export; retry later or select fewer datasets')
                reserved += extra
                chunks.append(message)
                if not message.get('more_body', False):
                    break
            index = 0
            async def bounded_receive():
                nonlocal index
                if index < len(chunks):
                    message = chunks[index]
                    index += 1
                    return message
                return await receive()
            await self.app(scope, bounded_receive, send)
        finally:
            with self.reservation_lock:
                self.reserved -= reserved
            self.active -= 1
