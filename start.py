"""Start the web application: python start.py."""

import os
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from dotenv import load_dotenv


def main():
    project_root = Path(__file__).resolve().parent
    os.chdir(project_root)
    load_dotenv(project_root / ".env")
    domain = urlsplit(os.getenv("domain", ""))
    if not domain.hostname or domain.path not in ("", "/") or domain.query or domain.fragment or domain.username:
        raise SystemExit("Set domain to the public HTTPS origin, without a path or credentials.")
    if domain.scheme != "https" and not (domain.scheme == "http" and domain.hostname in {"localhost", "127.0.0.1", "::1"}):
        raise SystemExit("domain must use HTTPS (HTTP is allowed only on localhost).")
    try:
        port = int(os.getenv("SERVER_PORT", os.getenv("PORT", "8000")))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise SystemExit("SERVER_PORT must be a port between 1 and 65535.") from None
    # Jobs and sessions share this process's memory; do not enable multiple workers.
    uvicorn.run("webserver:app", host="0.0.0.0", port=port, workers=1, access_log=False)


if __name__ == "__main__":
    main()
