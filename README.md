# Trakt To Pmdb Migration Utility

Migrate your Trakt data into The PublicMetaDb.

**[Open the migration website](https://trakt-migration.italianodoc.eu/)**

Web mode imports a Trakt export ZIP and pushes selected data to The PublicMetaDb.
The separate CLI connects to Trakt using the Device Code flow and stores its token locally.

## What it Migrates

- Watchlist
- Dropped shows from an explicit dropped-show dataset or shows selected by title from the export
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

- Python 3.10+
- A Trakt API app (client ID + client secret), for CLI use only
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
   a process restart also signs users out. A graceful shutdown waits for running
   jobs, but a forced restart or host termination can interrupt them.
   Closing the browser or disconnecting PMDB does not stop a running migration
   while the server process remains alive.

ZIP files may contain nested folders and split datasets. Resume points are read
directly from `watched-playback` without a Trakt login. Empty exported datasets
are valid; missing data for a selected stage is reported as a failure.

The interface reports completed, skipped and failed stages separately. Reaching
100% means processing has ended; success also requires every selected stage to
succeed. Partial failures preserve the export and preselect only failed or unrun
stages for a manual retry. Review PMDB first: a failed stage may already have
written some items, especially if an API response timed out. Writes are never
retried automatically. API requests use a 5-second connection timeout and a
30-second read timeout. Completed job results remain temporarily available for
reconnection while the process is running. Each open tab reads progress independently.

### Resume point outcomes

PMDB applies its own playback rules ([official API documentation](https://publicmetadb.com/api-docs)):

| Exported progress | PMDB outcome | Effect |
| --- | --- | --- |
| Below 2% | `ignored` | No resume point is saved. |
| 2% to below 80% | `saved` | The resume point is saved or updated. |
| 80% or more | `completed` | The resume point is cleared, including earlier episode resume points in the same show. |

The migration logs count these outcomes separately for each request. All three
are expected responses; actual errors, unknown outcomes and incomplete responses
still fail the stage. A successful stage therefore does not mean that every
resume point was saved. Playback percentages are preserved, never raised to bypass
PMDB's thresholds. Watch history is imported separately: a `completed` resume
response does not itself create a watched-history entry in this tool.
PMDB manages resume timestamps, so original pause dates cannot be backdated.
Repeating the same export will not change these threshold outcomes.

## Other scripts

- `main.py`: core API/auth/migration implementation.
- `debug.py`: utility that clears PublicMetaDb watch history entries.

Use `debug.py` carefully because it deletes watch history records from The PublicMetaDb.

## Notes and current behavior

- PublicMetaDb list creation for custom Trakt lists currently creates new PublicMetaDb lists as part of migration.
- Mapping from Trakt IDs to TMDB IDs is attempted when TMDB IDs are missing.
- Web migration progress and logs appear in the browser; console logging is configurable.
- CLI authorization tokens are stored in `token.json` for reuse and refresh. Treat this file as sensitive since it contains OAuth tokens.

## Troubleshooting

- Web connection errors:
	- Check your PMDB key and reconnect after a server restart or session expiry.
	- Open the exact public origin configured in `domain`; alternate hostnames fail the same-origin check.
- CLI `401` or auth errors:
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

Web credentials are retained encrypted in process memory and are never stored in cookies, files or
server environment variables. Cookies contain only opaque, one-hour session IDs.
Old encrypted credential cookies are rejected. Disconnecting invalidates the
session immediately; a running migration keeps its credential until completion.
Expired sessions are purged at least once per minute. State-changing requests
require a same-origin custom header, and credentials are omitted from responses
and redacted from job logs. Use one application worker because session and job
state live in process memory.

The CLI uses `.env` and `token.json` locally; never commit or share those files.
If a key was exposed on a previous deployment, revoke and replace it at its provider.
Retained credentials use authenticated encryption with a random key generated
for each process lifetime. Running-job registries and progress authorization use
owner digests. Linux startup disables core dumps and ordinary debugger attachment.
Plaintext is still required for outbound authentication and sensitive-log redaction.
This limits accidental disclosure of retained state; a fully compromised process
or privileged host can still recover credentials. Python does not guarantee
immediate zeroization of temporary plaintext copies.

### Browser privacy

Open progress streams recheck their session and close on logout or expiry.
The worker continues independently; reconnect with the same PMDB key to inspect it.
The browser sends only fields required by the selected stages. The export filename and Trakt profile stay local.
Exports expire 24 hours after import and logs expire independently 24 hours after their first entry (existing browser data gets
an initial 24-hour transition window). Cleanup runs on page load and every minute
while open; a closed browser cannot run cleanup. The manual delete button clears
exports and logs immediately but preserves the running-job reconnect link.
Downloaded logs and the original ZIP on disk are outside this cleanup.

### Automated security checks

GitHub Actions runs Python and browser regression tests, audits both dependency
files with pip-audit, and scans Git history with Gitleaks using redacted output.
It runs for main-branch pushes, pull requests and weekly; Dependabot proposes
weekly dependency and Action updates. Actions are pinned to commit hashes and
workflow permissions are read-only. Check the workflow result before deployment;
a scan cannot prove that the application is free of vulnerabilities.
