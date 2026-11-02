# Trakt To Pmdb Migration Utility

Migrate your Trakt data into The PublicMetaDb.

Web mode imports a Trakt export ZIP and pushes selected data to The PublicMetaDb.
The separate CLI connects to Trakt using the Device Code flow and stores its token locally.

## What it Migrates

- Watchlist
- Custom Trakt lists
- Movie and Show watch history
- Resume points (progress) for movies and shows

The interactive entrypoint is `user.py`.

## Planned

Planned features and improvements:

- Only migrate select lists.
- A desktop GUI so migration can be run without using the terminal.
- ~~A website interface for account connection and migration management.~~
- Packaged desktop builds:
	- Windows `.exe`
	- macOS `.dmg`

## Requirements

- Python 3.9+
- A Trakt API app (client ID + client secret)
- A PMDB API key

## Local User Setup

1. Clone this repository.
2. Create and activate a virtual environment.
3. Install dependencies.
4. Create a `.env` file from `.env.example`.

For the CLI migration flow (`user.py`), install from `requirements_user.txt`.
For the web UI (`webserver.py`), install from `requirements.txt`.

Example:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements_user.txt
# or, for webserver usage:
# pip install -r requirements.txt
cp .env.example .env
```

Then edit `.env`:

```env
pmdb_api_key=Required
trakt_client=Required
trakt_secret=Required
```

## Local User First run and authentication

Run:

```bash
python user.py
```

On first run:

1. The script requests a Trakt device code.
2. Your browser opens the Trakt verification page.
3. After approval, the token is written to `token.json`.

On later runs:

- If `token.json` is still valid, it is reused.
- If expired, it is refreshed automatically.

### How migration works

When you run `user.py`, you can either:

- Migrate everything in one go, or
- Choose each area individually:
	- Watchlist
	- Lists
	- Show watch history
	- Movie watch history
    - Show resume points
    - Movie resume points

## Webserver Usage

1. Install `requirements.txt` with Python 3.10 or later.
2. Set `domain` to the public HTTPS origin, or `http://127.0.0.1:8000` locally.
3. Run `python start.py` (`SERVER_PORT` is detected automatically, defaulting to 8000).
4. Open the site, connect your PMDB key, upload a Trakt export ZIP, select options,
   and start the migration. No Trakt OAuth or cookie encryption key is required.
5. Use Disconnect PMDB to invalidate your session. Sessions expire after one hour;
   restarting the process also signs users out. An already running migration
   continues until completion.

## Other scripts

- `main.py`: core API/auth/migration implementation.
- `debug.py`: utility that clears PublicMetaDb watch history entries.

Use `debug.py` carefully because it deletes watch history records from The PublicMetaDb.

## Notes and current behavior

- PublicMetaDb list creation for custom Trakt lists currently creates new PublicMetaDb lists as part of migration.
- Mapping from Trakt IDs to TMDB IDs is attempted when TMDB IDs are missing.
- Console output is the primary logging mechanism.
- Authorization tokens are stored in `token.json` for reuse and refresh. Treat this file as sensitive since it contains OAuth tokens.

## Troubleshooting

- `401` or auth errors:
	- Check `.env` values.
	- Delete `token.json` and run again to re-authorize.
- Items not migrating:
	- Confirm those items exist in Trakt and are visible for the authenticated account.
	- Check terminal output for PMDB API errors.
- Dependency import errors:
	- Re-activate your virtual environment.
	- Reinstall packages with the correct file:
		- `pip install -r requirements_user.txt` for CLI (`user.py`)
		- `pip install -r requirements.txt` for webserver (`webserver.py`)

## Security

Web credentials stay in process memory and are never stored in cookies, files or
server environment variables. Cookies contain only opaque, one-hour session IDs.
Old encrypted credential cookies are rejected. Disconnecting invalidates the
session immediately; a running migration keeps its credential until completion.
Expired sessions are purged at least once per minute. State-changing requests
require a same-origin custom header, and credentials are omitted from responses
and redacted from job logs. Use one application worker because session and job
state live in process memory.

The CLI uses `.env` and `token.json` locally; never commit or share those files.
If a key was exposed on a previous deployment, revoke and replace it at its provider.
A compromised running host can still access credentials being processed in memory.
