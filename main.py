import datetime
import math
import queue
from credentials import ProtectedHeaders

import requests
from dotenv import load_dotenv
import os
from dataclasses import dataclass
from webbrowser import open_new_tab
from time import sleep

# Load environment variables from .env file
load_dotenv()

trakt_api_url = "https://api.trakt.tv"
pmdb_api_url = "https://publicmetadb.com/api"
version = "1.1.2"
userAgent = f"TraktMigration/{version}"

REQUEST_TIMEOUT = (5, 30)


class BoundedSession(requests.Session):
    """Bound every API operation; writes are deliberately never retried."""

    def request(self, method, url, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = REQUEST_TIMEOUT
        return super().request(method, url, **kwargs)


session = BoundedSession()
session.headers.update({
    "Content-Type": "application/json",
    "User-Agent": userAgent
})

@dataclass
class SyncContext:
    pmdb_headers: dict
    trakt_data: dict
    trakt_profile: dict
    trakt_headers: dict | None = None
    event_queue: queue.Queue | None = None

    @property
    def username(self) -> str:
        return (self.trakt_profile or {}).get("username", "")
    
def log(message: str, ctx: SyncContext = None, level: str = "info") -> None:
    event_queue = ctx.event_queue if ctx else None
    username = ctx.username if ctx and ctx.username else "SYSTEM"
    sensitive_values = [os.getenv(name) for name in ("pmdb_api_key", "trakt_client", "trakt_secret", "cookie_encryption_key")]
    if ctx:
        for headers in (ctx.pmdb_headers, ctx.trakt_headers):
            if headers and headers.get("Authorization"):
                sensitive_values.append(headers["Authorization"].split(" ")[-1])
    for secret in sensitive_values:
        if secret:
            message = str(message).replace(secret, "[REDACTED]")
            username = str(username).replace(secret, "[REDACTED]")

    def print_to_console():
        print(f"{username}: [{level.upper()}] {message}")


    if event_queue and level.lower() != "verbose":
        try:
            event_queue.put_nowait({"type": "log", "message": message, "level": level})
        except queue.Full:
            pass

    if os.getenv("log_to_console", "false").lower() == "true" and level.lower() != "verbose":
        print_to_console()
    elif os.getenv("log_to_console", "false").lower() == "verbose":
        print_to_console()
    elif (os.getenv("domain", "").replace(" ", "") == "" or os.getenv("domain", "").lower() == "required_if_using_as_a_webserver_with_webserver.py"):
        print(f"{username}: [{level.upper()}] {message}")


def report_item_progress(ctx: SyncContext, completed: int, total: int, message: str) -> None:
    """Publish at most about 100 safe updates per loop, without per-item payloads."""
    if not ctx.event_queue or total <= 0:
        return
    stride = max(1, math.ceil(total / 100))
    if completed not in {0, 1, total} and completed % stride:
        return
    event = {"type": "item_progress", "completed": completed, "total": total, "message": message}
    if getattr(ctx, "migration_step", None) is not None:
        event["step"] = ctx.migration_step
    if getattr(ctx, "migration_stage", None) is not None:
        event["stage"] = ctx.migration_stage
    try:
        ctx.event_queue.put_nowait(event)
    except queue.Full:
        pass


def resolve_tmdb_id(ctx: SyncContext, media: dict, media_type: str):
    ids = media.get("ids") or {}
    if ids.get("tmdb"):
        return ids["tmdb"]
    if not ids.get("trakt"):
        log("Missing media identifiers; the item cannot be imported.", ctx=ctx, level="error")
        return None
    response = session.get(pmdb_api_url + "/external/mappings/lookup", headers=ctx.pmdb_headers,
                           params={"id_type": "trakt", "id_value": ids["trakt"], "media_type": media_type})
    if response.status_code == 200:
        for result in response.json().get("results") or []:
            if result.get("tmdb_id"):
                return result["tmdb_id"]
    log("Could not resolve a media identifier; the item was not imported.", ctx=ctx, level="error")
    return None

def create_trakt_headers(token_data: dict = None) -> dict:
    headers = {
        "trakt-api-version": "2",
        "trakt-api-key": os.getenv("trakt_client")
    }

    if token_data and token_data.get("access_token") and token_data.get("token_type"):
        headers["Authorization"] = f"{token_data.get('token_type')} {token_data.get('access_token')}"

    return headers

def build_sync_context(pmdb_api_key: str, event_queue: queue.Queue = None, trakt_data: dict = None) -> SyncContext:
    return SyncContext(
        pmdb_headers=ProtectedHeaders(pmdb_api_key),
        event_queue=event_queue,
        trakt_data=trakt_data,
        trakt_profile=trakt_data.get("user-profile") if trakt_data else None,
    )

def add_user_information(token_data: dict, trakt_headers: dict) -> dict | None:

    response = session.get(trakt_api_url + "/users/settings", headers=trakt_headers)
    if response.status_code == 200:
        user_info = response.json()
        #log(f"User information retrieved: {user_info.get('user').get('username')}")
        token_data["username"] = user_info.get("user").get("username")
        return token_data
    else:
        log(f"Failed to retrieve user information: {response.status_code}", level="error")
        return None
    
def check_pmdb_token(token: str) -> bool:
    global pmdb_api_url

    url = pmdb_api_url + "/external/ratings"
    querystring = {"tmdb_id":"550","media_type":"movie"}
    headers = {"Authorization": "Bearer " + token}

    try:
        response = requests.request("GET", url, headers=headers, params=querystring, timeout=20, allow_redirects=False)
    except requests.RequestException:
        log("PMDB validation request failed; sensitive details suppressed.", level="error")
        return False

    if not (response.status_code >= 200 and response.status_code < 300):
        log(f"PMDB token validation failed: {response.status_code}", level="error")

    return response.status_code >= 200 and response.status_code < 300

def code_authorize_user() -> dict | None:
    global trakt_api_url, userAgent
    log("Authorizing user...")

    client_id = os.getenv("trakt_client")
    client_secret = os.getenv("trakt_secret")
    
    payload = {"client_id": client_id}

    response = session.request("POST", trakt_api_url + "/oauth/device/code", json=payload)

    verification_url = response.json().get("verification_url")
    user_code = response.json().get("user_code")
    device_code = response.json().get("device_code")
    interval = response.json().get("interval")

    open_new_tab(verification_url + "?code=" + user_code)
    log(f"If the page doesn't open automatically, please visit {verification_url} and enter the code: {user_code}")

    while True:
        sleep(interval)
        payload = {
            "code": device_code,
            "client_id": client_id,
            "client_secret": client_secret
        }
        headers = {
            "Content-Type": "application/json",
            "User-Agent": userAgent
        }

        response = session.request("POST", trakt_api_url + "/oauth/device/token", json=payload, headers=headers)

        if response.status_code == 200:
            json_response = response.json()
            log("User authorized successfully!")

            trakt_auth_headers = create_trakt_headers(json_response)
            user_data = add_user_information(json_response, trakt_auth_headers)

            return user_data
        elif response.status_code == 400:
            log("Waiting for user authorization...")
        else:
            log(f"Error: {response.status_code}", level="error")
            break

def parse_listed_at(value: str) -> datetime.datetime:
    fallback = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
    if not isinstance(value, str) or not value:
        return fallback
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed.astimezone(datetime.timezone.utc)
    except ValueError:
        return fallback

def fetch_watchlist(ctx: SyncContext) -> list | None:
    log("Fetching watchlist...", ctx=ctx)
    if ctx.trakt_data is not None and ctx.trakt_data.get("lists-watchlist") is not None:
        log("Using watchlist from provided trakt_data.", ctx=ctx)
        return sorted(ctx.trakt_data["lists-watchlist"], key=lambda item: parse_listed_at(item.get("listed_at")))
    if not ctx.trakt_headers:
        log("Watchlist data is missing and no Trakt API credentials are available.", level="error", ctx=ctx)
        return None
    response = session.get(trakt_api_url + f"/users/{ctx.username}/watchlist/all/added/asc", headers=ctx.trakt_headers)
    if response.status_code == 200:
        return response.json()
    log(f"Failed to fetch watchlist: {response.status_code}", level="error", ctx=ctx)
    return None

def get_pmdb_watchlist_id(ctx: SyncContext) -> str | None:
    log("Retrieving PMDB watchlist ID...", ctx=ctx)
    url = pmdb_api_url + "/external/lists"

    response = session.get(url, headers=ctx.pmdb_headers)

    if response.status_code == 200:
        lists = response.json()
        for watchlist in lists.get("items", [{}]):
            if watchlist.get("type") == "watchlist":
                log(f"Found existing PMDB watchlist with ID: {watchlist.get('id')}", ctx=ctx)
                return watchlist.get("id")
        log("No existing PMDB watchlist found. A new one will be created.", ctx=ctx)

        url = pmdb_api_url + "/external/lists"
        body = {
            "name": "Watchlist",
            "is_public": True,
            "type": "watchlist"
        }
        response = session.post(url, headers=ctx.pmdb_headers, json=body)
        if response.status_code >= 200 and response.status_code < 300 and response.json().get("success") and (response.json().get("item") or {}).get("id"):
            new_watchlist = response.json()
            log(f"New PMDB watchlist created with ID: {new_watchlist.get('item').get('id')}", ctx=ctx)
            return new_watchlist.get("item").get("id")
        else:
            log(f"Failed to create PMDB watchlist: {response.status_code}", level="error", ctx=ctx)
        return None

def add_to_pmdb_list(ctx: SyncContext, pmdb_list_id: str, item: dict) -> bool:
    media_type = "movie" if item.get("type") == "movie" else "tv"
    media = item.get("movie", item.get("show", {}))
    tmdb_id = resolve_tmdb_id(ctx, media, media_type)
    if not pmdb_list_id or tmdb_id is None:
        return False
    response = session.post(pmdb_api_url + f"/external/lists/{pmdb_list_id}/items", headers=ctx.pmdb_headers,
                            json={"listId": pmdb_list_id, "media_type": media_type, "tmdb_id": tmdb_id})
    if 200 <= response.status_code < 300 and response.json().get("success"):
        return True
    log(f"Failed to add an item to PMDB list: {response.status_code}", level="error", ctx=ctx)
    return False

def sync_watchlist(ctx: SyncContext) -> bool:
    log("Syncing watchlist...", ctx=ctx)
    watchlist = fetch_watchlist(ctx)
    if watchlist is None:
        return False
    if not watchlist:
        log("No watchlist items to import.", ctx=ctx)
        return True
    pmdb_watchlist_id = get_pmdb_watchlist_id(ctx)
    if not pmdb_watchlist_id:
        log("Could not obtain the PMDB watchlist; no items were submitted.", level="error", ctx=ctx)
        return False
    all_success = True
    report_item_progress(ctx, 0, len(watchlist), "Importing watchlist items")
    for index, item in enumerate(watchlist, 1):
        if not add_to_pmdb_list(ctx, pmdb_watchlist_id, item):
            all_success = False
        report_item_progress(ctx, index, len(watchlist), "Importing watchlist items")
    log("Watchlist synced successfully!" if all_success else "Watchlist synced with some errors. Please check the logs for details.",
        level="info" if all_success else "error", ctx=ctx)
    return all_success

def fetch_trakt_lists(ctx: SyncContext) -> list | None:
    log("Fetching Trakt lists...", ctx=ctx)
    if ctx.trakt_data is not None and ctx.trakt_data.get("lists-lists") is not None:
        log("Using lists from provided trakt_data.", ctx=ctx)
        sorted_lists = []
        for trakt_list in ctx.trakt_data["lists-lists"]:
            if trakt_list.get("items") is not None:
                sorted_items = sorted(trakt_list["items"], key=lambda item: parse_listed_at(item.get("listed_at")))
                sorted_lists.append({**trakt_list, "items": sorted_items})
            else:
                sorted_lists.append(dict(trakt_list))
        return sorted_lists
    if not ctx.trakt_headers:
        log("Custom list data is missing and no Trakt API credentials are available.", level="error", ctx=ctx)
        return None
    response = session.get(trakt_api_url + f"/users/{ctx.username}/lists", headers=ctx.trakt_headers)
    if response.status_code == 200:
        return response.json()
    log(f"Failed to fetch Trakt lists: {response.status_code}", level="error", ctx=ctx)
    return None

def fetch_trakt_list(ctx: SyncContext, trakt_list: dict) -> list | None:
    if not ctx.trakt_headers:
        log("Custom list items are missing and no Trakt API credentials are available.", level="error", ctx=ctx)
        return None
    url = trakt_api_url + f"/users/{ctx.username}/lists/{(trakt_list.get('ids') or {}).get('trakt')}/items/all/added/asc"
    response = session.get(url, headers=ctx.trakt_headers)
    if response.status_code == 200:
        return response.json()
    log(f"Failed to fetch Trakt list: {response.status_code}", level="error", ctx=ctx)
    return None

def add_list_to_pmdb(ctx: SyncContext, trakt_list: dict, trakt_list_items: list) -> bool:
    log(f"Adding list '{trakt_list.get('name')}' to PMDB...", level="verbose", ctx=ctx)

    url = pmdb_api_url + "/external/lists"
    body = {
        "name": trakt_list.get("name"),
        "description": trakt_list.get("description"),
        "is_public": True if trakt_list.get("privacy") == "public" else False,
        "type": "custom"
    }

    response = session.post(url, headers=ctx.pmdb_headers, json=body)
    if response.status_code >= 200 and response.status_code < 300 and response.json().get("success") and (response.json().get("item") or {}).get("id"):
        pmdb_list = response.json()
        pmdb_list_id = pmdb_list.get("item").get("id")

        all_success = True

        report_item_progress(ctx, 0, len(trakt_list_items), "Importing custom list items")
        for index, item in enumerate(trakt_list_items, 1):
            success = add_to_pmdb_list(ctx, pmdb_list_id, item)
            if not success:
                all_success = False
                log(f"Failed to add item '{item.get('movie', item.get('show', {})).get('title')}' to PMDB list '{trakt_list.get('name')}'.", level="error", ctx=ctx)
            report_item_progress(ctx, index, len(trakt_list_items), "Importing custom list items")

        if not all_success:
            log(f"List '{trakt_list.get('name')}' added to PMDB with some errors. Please check the logs for details.", level="error", ctx=ctx)

        return all_success
    else:
        log(f"Failed to create PMDB list '{trakt_list.get('name')}': {response.status_code}", level="error", ctx=ctx)
        return False

def sync_lists(ctx: SyncContext, sync_all: bool = True) -> bool:
    trakt_lists = fetch_trakt_lists(ctx)
    if trakt_lists is None:
        return False
    if sync_all:
        log("Syncing all lists...", ctx=ctx)
    all_success = True
    report_item_progress(ctx, 0, len(trakt_lists), "Importing custom lists")
    for index, trakt_list in enumerate(trakt_lists, 1):
        if not sync_all:
            sync_list = input(f"Sync list '{trakt_list.get('name')}'? (y/n): ").strip().lower() == "y"
            if not sync_list:
                report_item_progress(ctx, index, len(trakt_lists), "Importing custom lists")
                continue
        trakt_list_items = trakt_list.get("items")
        if trakt_list_items is None:
            trakt_list_items = fetch_trakt_list(ctx, trakt_list)
        if trakt_list_items is None:
            all_success = False
            continue
        success = add_list_to_pmdb(ctx, trakt_list, trakt_list_items)
        if not success:
            all_success = False
        log("Custom list synced successfully!" if success else "Failed to sync a custom list. Check the logs for details.",
            level="info" if success else "error", ctx=ctx)
        report_item_progress(ctx, index, len(trakt_lists), "Importing custom lists")
    return all_success

def submit_watched_timestamp_to_pmdb(ctx: SyncContext, tmdb_id: int, type: str, watched_at: str, season: int = None, episode: int = None) -> bool:
    url = pmdb_api_url + "/external/watched"

    if watched_at == "1970-01-01T00:00:00.000Z":
        watched_at = None # For when Trakt marks a movie with 'Unknown' watched date.

    body = {
        "media_type": type,
        "tmdb_id": tmdb_id,
        "watched_at": watched_at
    }

    if season is not None and episode is not None:
        body["season"] = season
        body["episode"] = episode

    response = session.post(url, headers=ctx.pmdb_headers, json=body)
    if response.status_code >= 200 and response.status_code < 300 and response.json().get("success"):
        return True
    else:
        log(f"Failed to submit watch history for TMDB ID {tmdb_id} to PMDB: {response.status_code}", level="error", ctx=ctx)
        return False
    
def submit_history_movie_to_pmdb(ctx: SyncContext, movie: dict) -> bool:

    tmdb_id = resolve_tmdb_id(ctx, movie.get("movie", {}), "movie")
    if tmdb_id is None:
        return False

    if movie.get("history", []):
        all_success = True

        for watch in movie.get("history", []):
            watched_at = watch.get("watched_at")

            if watched_at:
                success = submit_watched_timestamp_to_pmdb(ctx, tmdb_id, "movie", watched_at)
                if not success:
                    all_success = False
                    log(f"Failed to submit watch history for movie '{movie.get('movie', {}).get('title')}' (TMDB ID: {tmdb_id}) to PMDB.", level="error", ctx=ctx)
            else:
                all_success = False
                log(f"No 'watched_at' timestamp found for movie '{movie.get('movie', {}).get('title')}' (TMDB ID: {tmdb_id}). Skipping.", level="error", ctx=ctx)

        return all_success
    else:
        return submit_watched_timestamp_to_pmdb(ctx, tmdb_id, "movie", movie.get("last_watched_at", "1970-01-01T00:00:00.000Z"))

def sync_movie_watch_history(ctx: SyncContext) -> bool:
    log("Syncing movie watch history...", ctx=ctx)

    if ctx.trakt_data and ctx.trakt_data.get("watched-history") is not None:
        log("Using watched movies from provided trakt_data.", ctx=ctx)
        watch_history = ctx.trakt_data.get("watched-history")
        success = submit_exported_history_to_pmdb(ctx, "movie", watch_history)
        if success:
            log("Movie watch history synced successfully!", ctx=ctx)
        else:
            log("Movie watch history failed to sync.", ctx=ctx)
        return success
    
    if not ctx.trakt_headers:
        log("Movie history data is missing and no Trakt API credentials are available.", level="error", ctx=ctx)
        return False

    url = trakt_api_url + f"/users/{ctx.username}/watched/movies"
    
    response = session.request("GET", url, headers=ctx.trakt_headers)

    all_success = True

    if response.status_code == 200:
        watched_movies = response.json()
        for movie in watched_movies:
            if movie.get("plays", 5) > 1:
                url = trakt_api_url + f"/users/{ctx.username}/history/movies/{movie.get('movie', {}).get('ids', {}).get('trakt')}"
                response = session.request("GET", url, headers=ctx.trakt_headers)
                if response.status_code == 200:
                    movie_watch_times = response.json()
                    movie["history"] = movie_watch_times
                else:
                    log(f"Failed to fetch watch history for movie '{movie.get('movie', {}).get('title')}' (Trakt ID: {movie.get('movie', {}).get('ids', {}).get('trakt')}): {response.status_code}", level="error", ctx=ctx)
                    all_success = False

            success = submit_history_movie_to_pmdb(ctx, movie)
            if not success:
                all_success = False
        return all_success

    else:
        log(f"Failed to fetch watched movies: {response.status_code}", level="error", ctx=ctx)
        return False
    
def add_show_watch_history(ctx: SyncContext, show: dict) -> dict:
    for season in show.get("seasons", []):

        season_details = None
        for episode in season.get("episodes", []):
            if episode.get("plays", 5) > 1:

                if not season_details:
                    url = trakt_api_url + f"/shows/{show.get('show', {}).get('ids', {}).get('trakt')}/seasons/{season.get('number')}"
                    response = session.request("GET", url, headers=ctx.trakt_headers)
                    if response.status_code == 200:
                        season_details = response.json()
                    else:
                        log(f"Failed to fetch season details for show '{show.get('show', {}).get('title')}' season {season.get('number')}: {response.status_code}", level="error", ctx=ctx)
                        continue

                for detailed_episode in season_details:
                    if detailed_episode.get("number", 0) == episode.get("number", 0):
                        url = trakt_api_url + f"/users/{ctx.username}/history/episodes/{detailed_episode.get('ids', {}).get('trakt')}"
                        response = session.request("GET", url, headers=ctx.trakt_headers)
                        if response.status_code == 200:
                            episode_watch_times = response.json()
                            episode["history"] = episode_watch_times
                        else:
                            log(f"Failed to fetch watch history for episode '{episode.get('title')}' (Trakt ID: {episode.get('ids', {}).get('trakt')}): {response.status_code}", level="error", ctx=ctx)

    return show

def submit_history_show_to_pmdb(ctx: SyncContext, show: dict) -> bool:
    tmdb_id = resolve_tmdb_id(ctx, show.get("show", {}), "tv")
    if tmdb_id is None:
        return False

    all_success = True

    for season in show.get("seasons", []):
        for episode in season.get("episodes", []):
            if episode.get("history", []):
                for watch in episode.get("history", []):
                    watched_at = watch.get("watched_at")

                    if watched_at:
                        success = submit_watched_timestamp_to_pmdb(ctx, tmdb_id, "tv", watched_at, season=season.get("number"), episode=episode.get("number"))
                        if not success:
                            all_success = False
                            log(f"Failed to submit watch history for episode '{episode.get('title')}' (TMDB ID: {tmdb_id}) to PMDB.", level="error", ctx=ctx)
                    else:
                        all_success = False
                        log(f"No 'watched_at' timestamp found for episode '{episode.get('title')}' (TMDB ID: {tmdb_id}). Skipping.", level="error", ctx=ctx)
            else:
                success = submit_watched_timestamp_to_pmdb(ctx, tmdb_id, "tv", episode.get("last_watched_at", "1970-01-01T00:00:00.000Z"), season=season.get("number"), episode=episode.get("number"))
                if not success:
                    all_success = False
                    log(f"Failed to submit watch history for episode '{episode.get('title')}' (TMDB ID: {tmdb_id}) to PMDB.", level="error", ctx=ctx)

    return all_success

def submit_exported_history_to_pmdb(ctx: SyncContext, media_type: str, history: list) -> bool:
    if media_type not in {"movie", "episode"}:
        log("Invalid media type for history export.", level="error", ctx=ctx)
        return False
    all_success = True
    total = sum(1 for item in history if item.get("type") == media_type)
    completed = 0
    report_item_progress(ctx, 0, total, "Importing watch history")
    for item in history:
        if item.get("type") != media_type:
            continue
        media = item.get("movie", item.get("show", {}))
        target_type = "movie" if media_type == "movie" else "tv"
        tmdb_id = resolve_tmdb_id(ctx, media, target_type)
        if tmdb_id is None:
            all_success = False
        else:
            episode = item.get("episode") or {}
            if media_type == "episode" and (episode.get("season") is None or episode.get("number") is None):
                log("Missing season or episode number; the history item was not imported.", level="error", ctx=ctx)
                all_success = False
            else:
                success = submit_watched_timestamp_to_pmdb(
                    ctx=ctx, tmdb_id=tmdb_id, type=target_type,
                    watched_at=item.get("watched_at", "1970-01-01T00:00:00.000Z"),
                    season=episode.get("season") if media_type == "episode" else None,
                    episode=episode.get("number") if media_type == "episode" else None)
                if not success:
                    all_success = False
        completed += 1
        report_item_progress(ctx, completed, total, "Importing watch history")
    return all_success

def sync_show_watch_history(ctx: SyncContext) -> bool:
    log("Syncing show watch history...", ctx=ctx)

    if ctx.trakt_data and ctx.trakt_data.get("watched-history") is not None:
        log("Using watched shows from provided trakt_data.", ctx=ctx)
        watch_history = ctx.trakt_data.get("watched-history")
        success = submit_exported_history_to_pmdb(ctx, "episode", watch_history)
        if success:
            log("Show watch history synced successfully!", ctx=ctx)
        else:
            log("Show watch history failed to sync.", level="error", ctx=ctx)
        return success

    if not ctx.trakt_headers:
        log("Show history data is missing and no Trakt API credentials are available.", level="error", ctx=ctx)
        return False

    url = trakt_api_url + f"/users/{ctx.username}/watched/shows"

    response = session.request("GET", url, headers=ctx.trakt_headers)

    if response.status_code == 200:
        watched_shows = response.json()

        all_success = True
        for show in watched_shows:
            show = add_show_watch_history(ctx, show)
            success = submit_history_show_to_pmdb(ctx, show)
            if not success:
                all_success = False
        
        if all_success:
            log("Show watch history synced successfully!", ctx=ctx)
        else:
            log("Show watch history synced with some errors. Please check the logs for details.", level="error", ctx=ctx)

        return all_success
    else:
        log(f"Failed to fetch watched shows: {response.status_code}", level="error", ctx=ctx)
        return False
    
def report_resume_outcomes(ctx: SyncContext, data: dict, expected: int) -> bool:
    """PMDB policy outcomes are handled successfully, but are not all saved."""
    counts = {"saved": 0, "ignored": 0, "completed": 0}
    if not isinstance(data, dict):
        log("Invalid PMDB resume response.", level="error", ctx=ctx)
        return False
    results = data.get("results")
    summary = data.get("summary", {})
    valid = True
    if results is not None:
        valid = isinstance(results, list) and len(results) == expected
        for result in results if isinstance(results, list) else []:
            action = result.get("action") if isinstance(result, dict) else None
            if isinstance(action, str) and action in counts:
                counts[action] += 1
            else:
                valid = False
        # Do not hide errors explicitly reported by the provider.
        if not isinstance(summary, dict) or summary.get("errors", 0) != 0:
            valid = False
        # If supplied, summary counts must agree with the per-item results.
        elif any(name in summary and (type(summary[name]) is not int or summary[name] != count)
                 for name, count in counts.items()):
            valid = False
    elif isinstance(summary, dict):
        for action in counts:
            value = summary.get(action, 0)
            if type(value) is not int or value < 0:
                valid = False
            else:
                counts[action] = value
        valid = valid and summary.get("errors", 0) == 0 and sum(counts.values()) == expected
    else:
        valid = False
    log(f"PMDB resume outcomes: {counts['saved']} saved, "
        f"{counts['ignored']} ignored (below 2% progress), "
        f"{counts['completed']} completed (at least 80%; resume point cleared).", ctx=ctx)
    if not valid:
        log("PMDB resume response contains errors, unknown outcomes or missing results.", level="error", ctx=ctx)
    return valid


def submit_resume_point_batch_to_pmdb(ctx: SyncContext, batch: list) -> bool:

    if not batch:
        return True

    submit_list = []        

    for item in batch:
        item_type = "movie" if item.get("type") == "movie" else "tv"
        item_spesific = item.get("movie", item.get("show", {}))
        ids = item_spesific.get("ids", {})

        # Normalize progress percentage to runtime and position in milliseconds for PMDB, since it doesn't support percentage-based resume points.
        percentage = item.get("progress", 0)
        runtime_ms = 100 * 10000 # Full 100% complete
        position_ms = percentage * 10000 # Normalized to account for decimal points
        position_ms = round(position_ms) # Round to nearest millisecond

        body = {
            "media_type": item_type,
            "tmdb_id": ids.get("tmdb"),
            "id_type": "trakt",
            "id_value": ids.get("trakt"),
            "position_ms": position_ms,
            "runtime_ms": runtime_ms
        }

        if item_type == "tv":
            body["season"] = item.get("episode", {}).get("season")
            body["episode"] = item.get("episode", {}).get("number")

        submit_list.append(body)

    response = session.post(pmdb_api_url + "/external/resume/batch", headers=ctx.pmdb_headers, json={"items": submit_list})
    if response.status_code >= 200 and response.status_code < 300:
        return report_resume_outcomes(ctx, response.json(), len(submit_list))
    else:
        log(f"Failed to submit resume points batch to PMDB: {response.status_code}", level="error", ctx=ctx)
        return False

def submit_resume_point_to_pmdb(ctx: SyncContext, item: dict) -> bool:

    item_type = "movie" if item.get("type") == "movie" else "tv"
    item_spesific = item.get("movie", item.get("show", {}))
    ids = item_spesific.get("ids", {})

    # Normalize progress percentage to runtime and position in milliseconds for PMDB, since it doesn't support percentage-based resume points.
    percentage = item.get("progress", 0)
    runtime_ms = 100 * 10000 # Full 100% complete
    position_ms = percentage * 10000 # Normalized to account for decimal points
    position_ms = round(position_ms) # Round to nearest millisecond

    url = pmdb_api_url + "/external/resume"
    body = {
        "media_type": item_type,
        "tmdb_id": ids.get("tmdb"),
        "id_type": "trakt",
        "id_value": ids.get("trakt"),
        "position_ms": position_ms,
        "runtime_ms": runtime_ms
    }

    if item_type == "tv":
        body["season"] = item.get("episode", {}).get("season")
        body["episode"] = item.get("episode", {}).get("number")

    response = session.post(url, headers=ctx.pmdb_headers, json=body)
    if response.status_code >= 200 and response.status_code < 300:
        return report_resume_outcomes(ctx, {"results": [response.json()]}, 1)
    else:
        media_label = "Movie" if item_type == "movie" else "Show"
        log(f"Failed to submit resume point for {media_label} '{item_spesific.get('title')}' (Trakt ID: {ids.get('trakt')}) to PMDB: {response.status_code}", level="error", ctx=ctx)
        return False

def sync_resume_points(ctx: SyncContext, sync_type: str) -> bool:
    log(f"Syncing {sync_type} resume points...", ctx=ctx)
    if sync_type not in {"movies", "episodes"}:
        log("Invalid resume-point media type.", level="error", ctx=ctx)
        return False
    singular = "movie" if sync_type == "movies" else "episode"
    if ctx.trakt_data is not None and ctx.trakt_data.get("watched-playback") is not None:
        log(f"Using {sync_type} resume points from provided trakt_data.", ctx=ctx)
        playback = ctx.trakt_data["watched-playback"]
    elif ctx.trakt_headers:
        response = session.get(trakt_api_url + f"/sync/playback/{sync_type}", headers=ctx.trakt_headers)
        if response.status_code != 200:
            log(f"Failed to fetch {sync_type} resume points: {response.status_code}", level="error", ctx=ctx)
            return False
        playback = response.json()
    else:
        log("Playback data is missing and no Trakt API credentials are available.", level="error", ctx=ctx)
        return False
    if not isinstance(playback, list):
        log("Playback data must be an array.", level="error", ctx=ctx)
        return False
    progress_data = sorted((item for item in playback if item.get("type") == singular),
                           key=lambda item: parse_listed_at(item.get("paused_at")))
    if not progress_data:
        log(f"No {sync_type} resume points to import.", ctx=ctx)
        return True
    all_success = True
    total = len(progress_data)
    report_item_progress(ctx, 0, total, "Importing resume points")
    for offset in range(0, total, 50):
        batch = progress_data[offset:offset + 50]
        if not submit_resume_point_batch_to_pmdb(ctx, batch):
            all_success = False
        report_item_progress(ctx, min(offset + len(batch), total), total, "Importing resume points")
    log(f"{sync_type.capitalize()} resume points synced successfully!" if all_success else
        f"{sync_type.capitalize()} resume points synced with some errors. Please check the logs for details.",
        level="info" if all_success else "error", ctx=ctx)
    return all_success

def submit_dropped_shows_to_pmdb(ctx: SyncContext, dropped_shows: list) -> bool:
    all_success = True
    report_item_progress(ctx, 0, len(dropped_shows), "Importing dropped shows")
    for index, show in enumerate(dropped_shows, 1):
        tmdb_id = resolve_tmdb_id(ctx, show.get("show", {}), "tv")
        if tmdb_id is None:
            all_success = False
        else:
            response = session.post(pmdb_api_url + "/external/dropped", headers=ctx.pmdb_headers,
                                    json={"media_type": "tv", "tmdb_id": tmdb_id})
            if not (200 <= response.status_code < 300 and response.json().get("success")):
                all_success = False
                log(f"Failed to submit a dropped show to PMDB: {response.status_code}", level="error", ctx=ctx)
        report_item_progress(ctx, index, len(dropped_shows), "Importing dropped shows")
    return all_success

def sync_dropped_shows(ctx: SyncContext) -> bool:
    log("Syncing dropped shows...", ctx=ctx)
    if ctx.trakt_data is None or ctx.trakt_data.get("hidden-dropped") is None:
        log("Dropped-show data is missing from the export.", level="error", ctx=ctx)
        return False
    dropped_shows = ctx.trakt_data["hidden-dropped"]
    if not dropped_shows:
        log("No dropped shows to import.", ctx=ctx)
        return True
    success = submit_dropped_shows_to_pmdb(ctx, dropped_shows)
    log("Dropped shows synced successfully!" if success else "Dropped shows failed to sync.",
        level="info" if success else "error", ctx=ctx)
    return success
