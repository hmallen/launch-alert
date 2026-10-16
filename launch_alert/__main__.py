"""CLI entry point, including device enrollment and delivery checks."""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
from http.client import HTTPConnection, HTTPException
from pathlib import Path

from .config import Config, load_env
from .notifier import NtfyNotifier


def initialize():
    path = Path(".env")
    if path.exists():
        print(".env already exists; leaving it intact.")
        return 0
    topic = "launch-" + secrets.token_urlsafe(24)
    content = f"NTFY_SERVER=https://ntfy.sh\nNTFY_TOPIC={topic}\nLEAD_MINUTES=30\nPOLL_SECONDS=300\nDISPLAY_TIMEZONE=America/Chicago\nDB_PATH=data/launch-alert.sqlite3\nHEALTH_HOST=127.0.0.1\nHEALTH_PORT=8080\n"
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write(content)
    print("Created .env with a random ntfy topic (keep this file private).")
    print(f"Subscribe each device to https://ntfy.sh/{topic}")
    print("Then run: python -m launch_alert test-notification")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Rocket launch livestream reminders via ntfy")
    parser.add_argument("command", choices=("init", "run", "once", "status", "healthcheck", "test-notification"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "init":
        return initialize()
    try:
        load_env(Path(os.environ.get("ENV_FILE", ".env")))
        config = Config.from_env()
        if args.command == "healthcheck":
            connection = HTTPConnection("127.0.0.1", config.health_port, timeout=3)
            try:
                connection.request("GET", "/healthz")
                response = connection.getresponse()
                if response.status != 200:
                    print(f"Worker unhealthy (HTTP {response.status}).")
                    return 1
                print("Worker healthy.")
                return 0
            finally:
                connection.close()
        if args.command == "test-notification":
            NtfyNotifier(config.ntfy_server, config.ntfy_topic, config.ntfy_token).send(
                {"title": "Launch alerts connected", "message": f"Device test: rocket launches with a listed livestream will trigger a reminder about {config.lead_minutes} minutes before launch. This is a test, not an upcoming launch.", "tags": ["rocket"], "priority": 4},
                "test-" + secrets.token_hex(12))
            print("ntfy accepted the test notification. Confirm it appeared on every subscribed device.")
            return 0
        from .service import report, run, utcnow
        if args.command == "status":
            if not config.db_path.exists():
                print("No state database yet; start the worker first.")
                return 1
            from .storage import Store
            store = Store(config.db_path)
            try:
                print(json.dumps(report(store, utcnow(), config.max_data_age), indent=2))
            finally:
                store.close()
            return 0
        return run(config, once=args.command == "once")
    except (ValueError, OSError, RuntimeError, HTTPException) as exc:
        # CLI errors can include locally configured paths; network errors remain sanitized in adapters.
        print(f"Error: {exc}", file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
