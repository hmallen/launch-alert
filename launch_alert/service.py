"""Long-running scheduler with source rate budgeting and a local health endpoint."""

from __future__ import annotations

import json
import logging
import signal
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .engine import Engine
from .model import timestamp
from .notifier import NtfyNotifier
from .provider import LaunchLibraryProvider, RateLimited
from .storage import Store, worker_lock

LOG = logging.getLogger("launch_alert")


def utcnow():
    return datetime.now(timezone.utc)


def report(store: Store, now: datetime, max_age: int = 900) -> dict:
    last = timestamp(store.get("last_fetch"))
    age = (now - last).total_seconds() if last else None
    pending = store.db.execute("SELECT COUNT(*),SUM(CASE WHEN attempts>0 THEN 1 ELSE 0 END) FROM events WHERE delivered_at IS NULL AND canceled_at IS NULL AND expires_at>?", (now.isoformat(),)).fetchone()
    next_fetch = store.get("next_fetch")
    return {"healthy": age is not None and 0 <= age <= max_age and not pending[1],
            "last_source_refresh": last.isoformat() if last else None,
            "source_age_seconds": round(age) if age is not None else None,
            "next_source_refresh": next_fetch,
            "source_error": store.get("source_error"),
            "last_notification_accepted": store.get("last_delivery"),
            "pending_notifications": pending[0], "retrying_notifications": pending[1] or 0,
            "tracked_launches": store.db.execute("SELECT COUNT(*) FROM launches WHERE present=1").fetchone()[0]}


class Health:
    def __init__(self, host: str, port: int):
        self.snapshot = {"healthy": False, "state": "starting"}
        self.checked_at = utcnow()
        self.max_age = 30
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path not in ("/healthz", "/status"):
                    self.send_error(404)
                    return
                # A live HTTP thread must not hide a wedged scheduler thread.
                snapshot = dict(owner.snapshot)
                if (utcnow() - owner.checked_at).total_seconds() > owner.max_age:
                    snapshot["healthy"] = False
                    snapshot["state"] = "scheduler heartbeat overdue"
                body = json.dumps(snapshot).encode()
                self.send_response(200 if snapshot["healthy"] or self.path == "/status" else 503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class Worker:
    def __init__(self, config, store, provider=None, notifier=None):
        self.config = config
        self.store = store
        self.engine = Engine(store, config.lead_minutes, config.max_data_age)
        self.provider = provider or LaunchLibraryProvider(config.ll2_base_url, config.ll2_token)
        if not config.ll2_token:
            self.provider.before_request = self.reserve_source_request
        self.notifier = notifier or NtfyNotifier(config.ntfy_server, config.ntfy_topic, config.ntfy_token)

    def reserve_source_request(self):
        """Persist every request before I/O, including pagination and failed requests."""
        now = utcnow()
        history = [timestamp(item) for item in self.store.get("source_requests", [])]
        history = [item for item in history if item and item > now - timedelta(hours=1)]
        if len(history) >= 15:
            seconds = max(1, int((min(history) + timedelta(hours=1) - now).total_seconds()) + 1)
            raise RateLimited("Local source request budget exhausted", seconds)
        with self.store.db:
            self.store.set("source_requests", [item.isoformat() for item in history] + [now.isoformat()])

    def tick(self, now=None):
        realtime = now is None
        now = now or utcnow()
        next_fetch = timestamp(self.store.get("next_fetch"))
        if next_fetch is None or next_fetch <= now:
            # Reserve source budget before I/O so restarts cannot hammer the free API.
            with self.store.db:
                self.store.set("next_fetch", (now + timedelta(seconds=self.config.poll_seconds)).isoformat())
            try:
                records = self.provider.fetch(now)
                # Data age starts at request initiation; slow pagination doesn't make old data fresh.
                self.engine.reconcile(records, now)
            except Exception as exc:
                delay = max(self.config.poll_seconds, getattr(exc, "retry_after", 0) or 0)
                count = max(1, getattr(self.provider, "last_request_count", 1))
                delay = max(delay, self.config.poll_seconds * count)
                with self.store.db:
                    self.store.set("next_fetch", (now + timedelta(seconds=delay)).isoformat())
                    self.store.set("source_error", type(exc).__name__)
                LOG.warning("Source refresh failed (%s); next attempt in %ss", type(exc).__name__, delay)
            else:
                # Every pagination request counts against the source rate limit.
                count = max(1, getattr(self.provider, "last_request_count", 1))
                with self.store.db:
                    self.store.set("next_fetch", (now + timedelta(seconds=self.config.poll_seconds * count)).isoformat())
                LOG.info("Refreshed %s launch records", len(records))
        # Independent delivery timing: polling every five minutes does not quantize reminders.
        current = max(now, utcnow()) if realtime else now
        self.engine.schedule(current)
        self.engine.deliver(self.notifier, current, self.config.timezone)


def run(config, once=False):
    with worker_lock(config.db_path):
        store = Store(config.db_path)
        worker = Worker(config, store)
        health = None
        stop = threading.Event()
        previous_handlers = {}
        try:
            if not once:
                health = Health(config.health_host, config.health_port)
                health.start()
                LOG.info("Worker started; %s-minute reminders, %ss source polling", config.lead_minutes, config.poll_seconds)
                for sig in (signal.SIGINT, signal.SIGTERM):
                    previous_handlers[sig] = signal.signal(sig, lambda *_: stop.set())
            while True:
                worker.tick()
                state = report(store, utcnow(), config.max_data_age)
                if once:
                    print(json.dumps(state, indent=2))
                    return 0 if state["healthy"] else 1
                health.snapshot = state
                health.checked_at = utcnow()
                # Allow up to one full bounded source request plus normal loop interval.
                health.max_age = 120
                if stop.wait(1):
                    return 0
        finally:
            if health:
                health.close()
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)
            store.close()
