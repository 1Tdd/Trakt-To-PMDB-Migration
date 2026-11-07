import os
import json
import math
import requests
from datetime import datetime
from main import (
    code_authorize_user, sync_lists, sync_resume_points, sync_movie_watch_history,
    sync_show_watch_history, sync_watchlist, add_user_information,
    create_trakt_headers, build_sync_context, trakt_api_url,
)

TOKEN_REFRESH_TIMEOUT = (5, 30)


def valid_authentication(data) -> bool:
    return isinstance(data, dict) and all(
        isinstance(data.get(field), str) and bool(data[field].strip())
        for field in ("access_token", "token_type", "username")
    )

def check_for_existing_token() -> dict | None:
    if not os.path.exists("token.json"):
        print("No existing token found. Please authorize the user.")
        return None
    try:
        with open("token.json", "r") as token_file:
            token_data = json.load(token_file)
        if not valid_authentication(token_data):
            print("Saved authentication is incomplete. Please authorize again.")
            return None
        created_at = float(token_data["created_at"])
        expires_in = float(token_data["expires_in"])
        if not math.isfinite(created_at) or not math.isfinite(expires_in) or expires_in <= 0:
            raise ValueError
    except (OSError, ValueError, TypeError, KeyError):
        print("Saved authentication could not be read. Please authorize again.")
        return None

    if datetime.now().timestamp() < created_at + expires_in - 60:
        print("Existing token found and is still valid.")
        return token_data
    if not token_data.get("refresh_token"):
        print("Saved authentication has expired. Please authorize again.")
        return None

    print("Existing token found but has expired. Refreshing token...")
    headers = create_trakt_headers()
    payload = {
        "client_id": os.getenv("trakt_client"),
        "client_secret": os.getenv("trakt_secret"),
        "refresh_token": token_data["refresh_token"],
        "grant_type": "refresh_token",
    }
    try:
        response = requests.post(trakt_api_url + "/oauth/token", json=payload, headers=headers,
                                 timeout=TOKEN_REFRESH_TIMEOUT, allow_redirects=False)
        if response.status_code != 200:
            print(f"Failed to refresh token: HTTP {response.status_code}.")
            return None
        new_token_data = response.json()
        if not isinstance(new_token_data, dict) or not new_token_data.get("access_token") or not new_token_data.get("token_type"):
            print("Token refresh returned incomplete authentication.")
            return None
        user_data = add_user_information(new_token_data, create_trakt_headers(new_token_data))
    except (requests.RequestException, ValueError, TypeError, KeyError):
        print("Token refresh failed; sensitive error details suppressed.")
        return None
    if not valid_authentication(user_data):
        print("Token refresh could not retrieve the Trakt profile.")
        return None
    print("Token refreshed successfully.")
    return user_data

def clean_bool_input(prompt: str) -> bool:
    return input(prompt).lower().replace(" ", "") == "y"

def run_cli() -> int:
    pmdb_api_key = (os.getenv("pmdb_api_key") or "").strip()
    if not pmdb_api_key:
        print("Set pmdb_api_key before starting the migration.")
        return 1
    try:
        user_data = check_for_existing_token()
        if not user_data:
            user_data = code_authorize_user()
    except (requests.RequestException, ValueError, TypeError, KeyError):
        print("Trakt authentication failed; sensitive error details suppressed.")
        return 1
    if not valid_authentication(user_data):
        print("Trakt authentication failed. No migration was started.")
        return 1
    try:
        # Serialize before opening the existing token file, so invalid data cannot truncate it.
        serialized_token = json.dumps(user_data, indent=4)
        with open("token.json", "w") as token_file:
            token_file.write(serialized_token)
    except (OSError, TypeError, ValueError):
        print("Authentication could not be saved. No migration was started.")
        return 1

    ctx = build_sync_context(pmdb_api_key)
    ctx.trakt_headers = create_trakt_headers(user_data)
    ctx.trakt_profile = {"username": user_data["username"]}

    sync_all = clean_bool_input("Do you want to sync all data (watchlist, lists, watch history, and resume points)? (y/n): ")

    sync_lists_choice = sync_show_watch_history_choice = sync_movie_watch_history_choice = sync_watchlist_choice = sync_movie_resume_points_choice = sync_show_resume_points_choice = True

    if not sync_all:
        sync_watchlist_choice = clean_bool_input("Do you want to sync your watchlist? (y/n): ")
        sync_lists_choice = clean_bool_input("Do you want to sync all your lists? (y/n): ")
        sync_show_watch_history_choice = clean_bool_input("Do you want to sync your show watch history? (y/n): ")
        sync_movie_watch_history_choice = clean_bool_input("Do you want to sync your movie watch history? (y/n): ")
        sync_show_resume_points_choice = clean_bool_input("Do you want to sync your show progress/resume points? (y/n): ")
        sync_movie_resume_points_choice = clean_bool_input("Do you want to sync your movie progress/resume points? (y/n): ")

    actions = (
        (sync_watchlist_choice, sync_watchlist),
        (sync_lists_choice, sync_lists),
        (sync_show_watch_history_choice, sync_show_watch_history),
        (sync_movie_watch_history_choice, sync_movie_watch_history),
        (sync_show_resume_points_choice, lambda context: sync_resume_points(context, "episodes")),
        (sync_movie_resume_points_choice, lambda context: sync_resume_points(context, "movies")),
    )
    success = True
    try:
        for selected, action in actions:
            if selected and action(ctx) is not True:
                success = False
    except Exception:
        print("Migration failed; sensitive error details suppressed.")
        return 1
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(run_cli())
