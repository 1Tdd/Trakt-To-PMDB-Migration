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

# Define SIGTERM handler
shutdown_requested = threading.Event()
sessions = SessionStore()

async def purge_expired_sessions():
    while True:
        await asyncio.sleep(60)
        sessions.purge()

@asynccontextmanager
async def lifespan(app: FastAPI):
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

app = FastAPI(lifespan=lifespan)
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
server_start_time = datetime.now()

def create_event_queue() -> queue.Queue:
    return queue.Queue(maxsize=500)

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

def add_job(job_id: str, event_queue: queue.Queue, pmdb_api_key: str) -> None:
    global running_jobs

    with jobs_lock:
        running_jobs.append({"job_id": job_id, "event_queue": event_queue, "pmdb_api_key": pmdb_api_key})
    
def search_running_jobs(pmdb_api_key: str) -> list:
    global running_jobs

    jobs = []
    with jobs_lock:
        jobs = [job for job in running_jobs if job["pmdb_api_key"] == pmdb_api_key]
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
                        "hidden-calendar": []
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
                    "version": "1.0.0",
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
    logged_in = session_key(request) is not None
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
    try:
        if sync_options.get("sync_watchlist_choice"):
            sync_watchlist(sync_context)
        sync_context.trakt_data.pop("lists-watchlist", None)
        enqueue_event(event_queue, {"type": "progress", "message": "Finished syncing watchlist", "step": 1, "progress": 14})

        if sync_options.get("sync_lists_choice"):
            sync_lists(sync_context)
        sync_context.trakt_data.pop("lists-lists", None)
        enqueue_event(event_queue, {"type": "progress", "message": "Finished syncing lists", "step": 2, "progress": 29})

        if sync_options.get("sync_show_watch_history_choice"):
            sync_show_watch_history(sync_context)
        enqueue_event(event_queue, {"type": "progress", "message": "Finished syncing show watch history", "step": 3, "progress": 43})

        if sync_options.get("sync_movie_watch_history_choice"):
            sync_movie_watch_history(sync_context)
        sync_context.trakt_data.pop("watched-history", None)
        enqueue_event(event_queue, {"type": "progress", "message": "Finished syncing movie watch history", "step": 4, "progress": 57})

        if sync_options.get("sync_dropped_shows_choice"):
            sync_dropped_shows(sync_context)
        sync_context.trakt_data.pop("hidden-calendar", None)
        enqueue_event(event_queue, {"type": "progress", "message": "Finished syncing dropped shows", "step": 5, "progress": 71})

        if sync_options.get("sync_show_resume_points_choice"):
            sync_resume_points(sync_context, "episodes")
        enqueue_event(event_queue, {"type": "progress", "message": "Finished syncing show resume points", "step": 6, "progress": 86})
            
        if sync_options.get("sync_movie_resume_points_choice"):
            sync_resume_points(sync_context, "movies")
        sync_context.trakt_data.pop("watched-playback", None)

        enqueue_event(event_queue, {"type": "complete", "message": "Migration complete", "step": 7, "progress": 100})
    except Exception:
        # Exception text and tracebacks may contain upstream credentials or request data.
        print("Migration failed; sensitive exception details suppressed.")
        enqueue_event(event_queue, {"type": "critical", "message": "Migration failed. Check PMDB availability and your export, then retry."})
    finally:
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

    trakt_auth = sync_options.trakt_data.get("user-profile", None)
    if not trakt_auth:
        raise HTTPException(status_code=401, detail="Not authenticated with Trakt or missing Trakt user profile data")
    
    existing_jobs = search_running_jobs(pmdb_api_key)
    if existing_jobs:
        response.status_code = status.HTTP_409_CONFLICT
        return {"success": True, "job_id": existing_jobs[0].get("job_id"), "events_url": f"/migrate/{existing_jobs[0].get('job_id')}/events", "message": "A migration job is already running for this PMDB account. Please wait for it to complete before starting a new one."}
    
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
    if job:
        if job["pmdb_api_key"] != pmdb_api_key:
            raise HTTPException(status_code=403, detail="Forbidden: You do not have access to this job's events")
    else:
        raise HTTPException(status_code=404, detail="Job not found")

    event_queue = job["event_queue"]

    async def event_generator() -> any:
        last_event_time = datetime.now()
        while True:
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.to_thread(event_queue.get, True, 1)  # Wait for an event with a timeout to allow checking for disconnects
                last_event_time = datetime.now()
                yield f"data: {json.dumps(event)}\n\n"
                event_type = event.get("type")
                if event_type in ["complete", "critical"]:
                    break  # Stop streaming after terminal events
            except queue.Empty:
                if (datetime.now() - last_event_time).total_seconds() > 15:  # Send a keep-alive comment every 30 seconds if no events have been sent
                    yield ": keep-alive\n\n"
                    last_event_time = datetime.now()

    return StreamingResponse(event_generator(), media_type="text/event-stream")

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
