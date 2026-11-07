import os
import json
from time import sleep
import queue
import threading
from datetime import datetime
from main import check_pmdb_token, sync_lists, sync_resume_points, sync_movie_watch_history, sync_show_watch_history, sync_dropped_shows, sync_watchlist, build_sync_context, version
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Response, status, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
from uuid import uuid4
from auth_sessions import SessionStore, SESSION_TTL_SECONDS
from urllib.parse import urlsplit
from contextlib import asynccontextmanager
import asyncio
import gc
import ctypes
import hashlib
import hmac
import time
from collections import OrderedDict, deque
from requests import RequestException
from process_security import harden_process
from resource_limits import RequestLimits, RateBudget, MAX_ACTIVE_JOBS

# Define SIGTERM handler
shutdown_requested = threading.Event()
sessions = SessionStore()

async def purge_expired_sessions():
    while True:
        await asyncio.sleep(60)
        sessions.purge()
        purge_completed_jobs()

@asynccontextmanager
async def lifespan(app: FastAPI):
    harden_process()
    shutdown_requested.clear()
    cleanup_task = asyncio.create_task(purge_expired_sessions())
    yield
    shutdown_requested.set()
    print("Shutdown signal received, waiting for running jobs to complete...")

    while True:
        with jobs_lock:
            remaining = len(running_jobs)
        if remaining == 0:
            print("All migration jobs completed, shutting down.")
            break
        print(f"Shutdown waiting for {remaining} running migration job(s)...")
        await asyncio.sleep(5)  # Wait before checking again
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    sessions.clear()
    with jobs_lock:
        completed_jobs.clear()

app = FastAPI(lifespan=lifespan)
app.add_middleware(RequestLimits)
auth_budget = RateBudget()
load_dotenv()
public_origin = os.getenv("domain", "http://127.0.0.1:8000").rstrip("/")
secure_cookies = urlsplit(public_origin).scheme == "https"
session_cookie_name = "__Host-pmdb_session" if secure_cookies else "pmdb_session"

@app.middleware("http")
async def protect_browser_requests(request: Request, call_next):
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("origin")
        fetch_site = request.headers.get("sec-fetch-site")
        if (request.headers.get("x-requested-with") != "TraktMigration"
                or (origin is not None and origin != public_origin)
                or fetch_site not in {None, "same-origin", "none"}):
            response = JSONResponse({"detail": "Same-origin request required"}, status_code=403)
        else:
            response = await call_next(request)
    else:
        response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; img-src 'self' data:; worker-src 'self' blob:; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    )
    return response

def session_key(request: Request):
    return sessions.get(request.cookies.get(session_cookie_name))

def delete_auth_cookies(response: Response):
    response.delete_cookie("pmdb_auth", path="/", secure=secure_cookies, httponly=True, samesite="strict")
    response.delete_cookie(session_cookie_name, path="/", secure=secure_cookies, httponly=True, samesite="strict")
running_jobs = []  # List to keep track of running jobs and their event queues
jobs_lock = threading.Lock()  # Lock to synchronize access to the running_jobs list
# Keep the existing-job check and registration atomic across concurrent requests.
# This lock is separate because a newly started worker also acquires jobs_lock.
job_start_lock = threading.Lock()
# Keep only safe terminal summaries so a fast job can be inspected after reconnect.
COMPLETED_JOB_TTL = 10 * 60
MAX_COMPLETED_JOBS = 32
completed_jobs = OrderedDict()
server_start_time = datetime.now()

class ReplayEventQueue(queue.Queue):
    """Bounded recent history; each SSE connection owns its own cursor."""

    def __init__(self, maxsize=500):
        super().__init__(maxsize=maxsize)
        self.history = deque(maxlen=maxsize)
        self.sequence = 0

    def put(self, item, block=True, timeout=None):
        with self.not_empty:
            if self.maxsize > 0 and self._qsize() >= self.maxsize:
                self._get()
                self.unfinished_tasks -= 1
            self._put(item)
            self.unfinished_tasks += 1
            self.sequence += 1
            self.history.append((self.sequence, item))
            self.not_empty.notify()

    def read_after(self, cursor):
        with self.mutex:
            return [(sequence, event) for sequence, event in self.history if sequence > cursor]


def create_event_queue() -> queue.Queue:
    return ReplayEventQueue(maxsize=500)

def enqueue_event(event_queue: queue.Queue, event: dict) -> None:
    try:
        event_queue.put_nowait(event)
    except queue.Full:
        # Terminal events must not be dropped or an SSE stream can remain alive forever.
        if event.get("type") in {"complete", "critical"}:
            try:
                event_queue.get_nowait()
                event_queue.put_nowait(event)
            except (queue.Empty, queue.Full):
                pass

def release_job_memory(sync_context) -> None:
    """Drop large job-owned objects and return free arenas to libc when supported."""
    sync_context.trakt_data = None
    sync_context.pmdb_headers = None
    sync_context.trakt_headers = None
    sync_context.event_queue = None
    sync_context.trakt_profile = None
    gc.collect()
    try:
        ctypes.CDLL(None).malloc_trim(0)
    except (AttributeError, OSError, TypeError):
        pass

def get_running_job(job_id: str) -> dict | None:
    global running_jobs

    with jobs_lock:
        job = next((job for job in running_jobs if job["job_id"] == job_id), None)
    return job

def remove_job(job_id: str) -> None:
    global running_jobs

    with jobs_lock:
        running_jobs = [job for job in running_jobs if job["job_id"] != job_id]

def job_owner_digest(pmdb_api_key: str) -> str:
    return hashlib.sha256(pmdb_api_key.encode()).hexdigest()


def purge_completed_jobs() -> None:
    with jobs_lock:
        now = time.monotonic()
        expired = [job_id for job_id, job in completed_jobs.items() if job["expires_at"] <= now]
        for job_id in expired:
            completed_jobs.pop(job_id, None)


def get_completed_job(job_id: str) -> dict | None:
    purge_completed_jobs()
    with jobs_lock:
        return completed_jobs.get(job_id)


def finish_job(job_id: str, terminal_event: dict) -> None:
    """Release credential-bearing job state, retaining only a bounded safe summary."""
    global running_jobs
    with jobs_lock:
        job = next((item for item in running_jobs if item["job_id"] == job_id), None)
        if job is not None:
            completed_jobs[job_id] = {
                "owner_digest": job["owner_digest"],
                "terminal_event": terminal_event,
                "expires_at": time.monotonic() + COMPLETED_JOB_TTL,
            }
            completed_jobs.move_to_end(job_id)
            while len(completed_jobs) > MAX_COMPLETED_JOBS:
                completed_jobs.popitem(last=False)
        running_jobs = [item for item in running_jobs if item["job_id"] != job_id]


def add_job(job_id: str, event_queue: queue.Queue, pmdb_api_key: str) -> None:
    global running_jobs

    with jobs_lock:
        running_jobs.append({"job_id": job_id, "event_queue": event_queue, "owner_digest": job_owner_digest(pmdb_api_key)})
    
def search_running_jobs(pmdb_api_key: str) -> list:
    global running_jobs

    jobs = []
    with jobs_lock:
        jobs = [job for job in running_jobs if hmac.compare_digest(job["owner_digest"], job_owner_digest(pmdb_api_key))]
    return jobs

class sync_options(BaseModel):
    sync_lists_choice: bool = False
    sync_movie_resume_points_choice: bool = False
    sync_movie_watch_history_choice: bool = False
    sync_show_resume_points_choice: bool = False
    sync_dropped_shows_choice: bool = False
    sync_show_watch_history_choice: bool = False
    sync_watchlist_choice: bool = False
    trakt_data: dict

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "sync_lists_choice": True,
                    "sync_movie_resume_points_choice": True,
                    "sync_movie_watch_history_choice": False,
                    "sync_show_resume_points_choice": True,
                    "sync_dropped_shows_choice": False,
                    "sync_show_watch_history_choice": True,
                    "sync_watchlist_choice": False,
                    "trakt_data": {
                        "watched-history": [],
                        "lists-watchlist": [],
                        "lists-lists": [],
                        "watched-playback": [],
                        "user-profile": [],
                        "hidden-dropped": []
                    }
                }
            ]
        }
    }

class server_status(BaseModel):
    status: str
    version: str
    server_time: str
    server_uptime_seconds: float

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "status": "ok",
                    "version": version,
                    "server_time": "2026-04-13T13:19:44.808877",
                    "server_uptime_seconds": 272.221156,
                }
            ]
        }
    }

@app.get("/status")
def get_server_status() -> server_status:
    global trakt_api_url, userAgent

    returnDict = {
        "status": "ok",
        "version": version,
        "server_time": datetime.now().isoformat(),
        "server_uptime_seconds": (datetime.now() - server_start_time).total_seconds()
    }

    status_return = server_status(**returnDict)

    return status_return
    
@app.post("/pmdb/auth")
def authenticate_pmdb_user(request: Request, response: Response, Authorization: str = Header(default=None)) -> dict:
    if not auth_budget.allow():
        raise HTTPException(status_code=429, detail="Too many connection attempts; retry in one minute",
                            headers={"Retry-After": "60"})
    scheme, _, api_key = (Authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not api_key or len(api_key) > 4096:
        raise HTTPException(status_code=401, detail="Missing or invalid API key")
    if not check_pmdb_token(api_key):
        raise HTTPException(status_code=401, detail="Invalid PMDB API key or PMDB unavailable")
    sessions.revoke(request.cookies.get(session_cookie_name))
    try:
        token = sessions.create(api_key)
    except OverflowError:
        raise HTTPException(status_code=503, detail="Too many active sessions; try again later") from None
    delete_auth_cookies(response)
    response.set_cookie(key=session_cookie_name, value=token, httponly=True,
                        max_age=SESSION_TTL_SECONDS, samesite="strict", secure=secure_cookies, path="/")
    return {"success": True, "message": "PMDB authentication successful"}


@app.get("/auth/status")
def get_authentication_status(request: Request, response: Response) -> dict:
    logged_in = sessions.owner(request.cookies.get(session_cookie_name)) is not None
    # Old encrypted credential cookies are deliberately never decoded or accepted.
    if "pmdb_auth" in request.cookies:
        response.delete_cookie("pmdb_auth", path="/", secure=secure_cookies, httponly=True, samesite="strict")
    return {"pmdb": logged_in}


@app.post("/pmdb/logout")
def logout_pmdb(request: Request, response: Response) -> dict:
    sessions.revoke(request.cookies.get(session_cookie_name))
    delete_auth_cookies(response)
    return {"success": True}


def migrate_data(sync_context: dict, sync_options: dict, event_queue: queue.Queue, job_id: str) -> None:
    stages = [
        ("watchlist", "sync_watchlist_choice", "watchlist", lambda: sync_watchlist(sync_context), "lists-watchlist"),
        ("lists", "sync_lists_choice", "lists", lambda: sync_lists(sync_context), "lists-lists"),
        ("show-history", "sync_show_watch_history_choice", "show watch history", lambda: sync_show_watch_history(sync_context), None),
        ("movie-history", "sync_movie_watch_history_choice", "movie watch history", lambda: sync_movie_watch_history(sync_context), "watched-history"),
        ("dropped-shows", "sync_dropped_shows_choice", "dropped shows", lambda: sync_dropped_shows(sync_context), "hidden-dropped"),
        ("show-resume", "sync_show_resume_points_choice", "show resume points", lambda: sync_resume_points(sync_context, "episodes"), None),
        ("movie-resume", "sync_movie_resume_points_choice", "movie resume points", lambda: sync_resume_points(sync_context, "movies"), "watched-playback"),
    ]
    results = []
    current_stage = None
    terminal_event = None
    try:
        for step, (stage_id, option, label, execute, release_key) in enumerate(stages, 1):
            current_stage = {"id": stage_id, "option": option, "name": label, "step": step}
            sync_context.migration_step = step
            sync_context.migration_stage = label
            stage_status = "skipped"
            if sync_options.get(option):
                try:
                    stage_status = "completed" if execute() is True else "failed"
                except RequestException:
                    # A timed-out write may already have been applied upstream. Never retry it automatically.
                    stage_status = "failed"
                    enqueue_event(event_queue, {"type": "log", "level": "error", "message": f"The {label} request failed or timed out. Review PMDB before retrying this stage."})
            result = {**current_stage, "status": stage_status}
            results.append(result)
            if release_key:
                sync_context.trakt_data.pop(release_key, None)
            message = f"Finished syncing {label}" if stage_status == "completed" else f"{label.capitalize()} {stage_status}"
            enqueue_event(event_queue, {"type": "progress", "message": message, "step": step,
                                       "stage": label, "status": stage_status, "steps": list(results),
                                       "progress": round(step / len(stages) * 100)})
        failed_steps = [item["option"] for item in results if item["status"] == "failed"]
        terminal_event = {"type": "complete", "success": not failed_steps,
                          "message": "Migration complete" if not failed_steps else "Migration finished with failed stages. Review the summary before retrying.",
                          "step": len(stages), "progress": 100, "steps": results, "failed_steps": failed_steps}
        enqueue_event(event_queue, terminal_event)
    except Exception:
        # Exception text and tracebacks may contain upstream credentials or request data.
        print("Migration failed; sensitive exception details suppressed.")
        if current_stage:
            results = [item for item in results if item["step"] != current_stage["step"]]
            results.append({**current_stage, "status": "failed"})
        for step, (stage_id, option, label, _, _) in enumerate(stages, 1):
            if step > len(results):
                results.append({"id": stage_id, "option": option, "name": label, "step": step,
                                "status": "not-run" if sync_options.get(option) else "skipped"})
        terminal_event = {"type": "critical", "success": False,
                          "message": "Migration aborted. Review the completed stages and PMDB before retrying.",
                          "steps": results,
                          "failed_steps": [item["option"] for item in results if item["status"] in {"failed", "not-run"}]}
        enqueue_event(event_queue, terminal_event)
    finally:
        if terminal_event is not None:
            finish_job(job_id, terminal_event)
        else:
            remove_job(job_id)
        release_job_memory(sync_context)


def create_sync_job(sync_context: dict, sync_options: dict, event_queue: queue.Queue) -> tuple[str, queue.Queue, threading.Thread]:
    job_id = f"job_{uuid4()}_{int(datetime.now().timestamp())}"  # Create a unique job ID based on the current timestamp and number of running jobs
    thread = threading.Thread(target=migrate_data, args=(sync_context, sync_options, event_queue, job_id))
    return job_id, event_queue, thread

# Used for testing the event streaming without running the actual migration logic
def create_sync_job_dummy() -> tuple[str, queue.Queue, threading.Thread]:
    job_id = f"job_{uuid4()}_{int(datetime.now().timestamp())}"  # Create a unique job ID based on the current timestamp and number of running jobs
    print(f"Created dummy job with ID:\n{job_id}\nJob URL:\n/migrate/{job_id}/events")
    event_queue = create_event_queue()
    add_job(job_id, event_queue, os.getenv("PMDB_API_KEY"))  # Add the new job to the list of running jobs with a dummy PMDB API key

    def dummy_event_generator() -> None:
        for i in range(6):
            enqueue_event(event_queue, {"type": "progress", "message": f"Dummy progress update {i+1}/6", "step": i+1, "progress": round((i+1)/6 * 100, 0), "complete": True if i == 5 else False})
            sleep(15)  # Simulate time taken for each step of the migration
        enqueue_event(event_queue, {"type": "complete", "message": "Dummy migration complete"})
        remove_job(job_id)  # Remove the job from the running jobs list after completion

    # Create a thread that simulates sending events to the queue
    thread = threading.Thread(target=dummy_event_generator)
    thread.start()
    return job_id, event_queue, thread

@app.post("/migrate")
def request_data_migration(sync_options: sync_options, response: Response, request: Request) -> dict:
    if shutdown_requested.is_set():
        raise HTTPException(status_code=503, detail="Server is shutting down; new migrations are disabled")

    pmdb_api_key = session_key(request)
    if not pmdb_api_key:
        raise HTTPException(status_code=401, detail="Not authenticated with PMDB")

    sync_options.trakt_data.pop("user-profile", None)  # Older clients may still send a profile.
    with job_start_lock:
        return start_data_migration(sync_options, response, pmdb_api_key)


def start_data_migration(sync_options: sync_options, response: Response, pmdb_api_key: str) -> dict:
    """Called with job_start_lock held so an owner cannot start overlapping jobs."""
    if shutdown_requested.is_set():
        raise HTTPException(status_code=503, detail="Server is shutting down; new migrations are disabled")
    existing_jobs = search_running_jobs(pmdb_api_key)
    if existing_jobs:
        response.status_code = status.HTTP_409_CONFLICT
        return {"success": True, "job_id": existing_jobs[0].get("job_id"), "events_url": f"/migrate/{existing_jobs[0].get('job_id')}/events", "message": "A migration job is already running for this PMDB account. Please wait for it to complete before starting a new one."}
    
    with jobs_lock:
        if len(running_jobs) >= MAX_ACTIVE_JOBS:
            raise HTTPException(status_code=503, detail="Migration capacity reached; retry later")
    sync_context = None
    job_id = None
    try:
        event_queue = create_event_queue()  # Create a bounded event queue for this job
        sync_context = build_sync_context(pmdb_api_key, event_queue, sync_options.trakt_data)  # Build the sync context with the provided options and event queue

        # Do not duplicate the potentially huge export into the worker's options.
        sync_options_data = sync_options.model_dump(exclude={"trakt_data"})
        job_id, event_queue, thread = create_sync_job(sync_context, sync_options_data, event_queue)  # Create the sync job and get the event queue
        add_job(job_id, event_queue, pmdb_api_key)  # Add the new job to the list of running jobs

        thread.start()

        return {"success": True, "job_id": job_id, "events_url": f"/migrate/{job_id}/events", "message": "Migration job started successfully"}
    except Exception:
        print("Error starting migration job; sensitive exception details suppressed.")
        if job_id is not None:
            remove_job(job_id)
        if sync_context is not None:
            release_job_memory(sync_context)
        raise HTTPException(status_code=500, detail="Failed to start migration job")

def stream_sync_job(job_id: str, pmdb_api_key: str, request: Request) -> StreamingResponse:
    job = get_running_job(job_id)
    completed = get_completed_job(job_id) if job is None else None
    if job is None and completed is None:
        raise HTTPException(status_code=404, detail="Job not found")
    owner_digest = job["owner_digest"] if job else completed["owner_digest"]
    if not hmac.compare_digest(owner_digest, job_owner_digest(pmdb_api_key)):
        raise HTTPException(status_code=403, detail="Forbidden: You do not have access to this job's events")

    # The long-lived generator retains only digests and the opaque session cookie.
    del pmdb_api_key
    def authorized():
        owner = sessions.owner(request.cookies.get(session_cookie_name))
        return owner is not None and hmac.compare_digest(owner_digest, owner)

    async def event_generator():
        if not authorized():
            yield 'event: session_expired\ndata: {}\n\n'
            return
        if completed is not None:
            yield f"data: {json.dumps(completed['terminal_event'])}\n\n"
            return
        event_queue = job["event_queue"]
        try:
            cursor = max(0, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            cursor = 0
        last_event_time = time.monotonic()
        while True:
            if not authorized():
                yield 'event: session_expired\ndata: {}\n\n'
                return
            if await request.is_disconnected():
                break
            if isinstance(event_queue, ReplayEventQueue):
                pending = event_queue.read_after(cursor)
            else:
                # Compatibility with injected queues and the local dummy worker.
                try:
                    pending = [(cursor + 1, event_queue.get_nowait())]
                except queue.Empty:
                    pending = []
            for sequence, event in pending:
                if not authorized():
                    yield 'event: session_expired\ndata: {}\n\n'
                    return
                cursor = sequence
                last_event_time = time.monotonic()
                yield f"id: {sequence}\ndata: {json.dumps(event)}\n\n"
                if event.get("type") in {"complete", "critical"}:
                    return
            if not pending:
                finished = get_completed_job(job_id)
                if finished is not None:
                    yield f"data: {json.dumps(finished['terminal_event'])}\n\n"
                    break
                if time.monotonic() - last_event_time > 15:
                    yield ": keep-alive\n\n"
                    last_event_time = time.monotonic()
                await asyncio.sleep(0.1)

    return StreamingResponse(event_generator(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})


@app.get("/migrate/{job_id}/events")
def migrate_job_events(job_id: str, request: Request) -> StreamingResponse:
    pmdb_api_key = session_key(request)
    if not pmdb_api_key:
        raise HTTPException(status_code=401, detail="Not authenticated with PMDB")
    return stream_sync_job(job_id, pmdb_api_key, request)

# This mounts the "static" directory to serve static files (like the callback HTML page) at the root URL.
# `html=True` makes `/` resolve to `static/index.html` automatically.
app.mount("/", StaticFiles(directory="static", html=True), name="static")

#create_sync_job_dummy()  # Create a dummy job on startup to test the event streaming without running the actual migration logic
