"""ntfy JSON publishing with bounded requests and validated acknowledgments."""

from __future__ import annotations

import json
import socket
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler


class DeliveryError(RuntimeError):
    retry_after = None


class DeliveryRateLimited(DeliveryError):
    def __init__(self, seconds):
        super().__init__("ntfy rate limit")
        self.retry_after = seconds


class NoRedirect(HTTPRedirectHandler):
    # Do not redirect authenticated publishing requests to a different destination.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def retry_seconds(value: str | None, now: datetime) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        try:
            return max(1, int((parsedate_to_datetime(value) - now).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return 300


class NtfyNotifier:
    def __init__(self, server, topic, token=None, timeout=15, opener=None):
        self.server = server
        self.topic = topic
        self.token = token
        self.timeout = timeout
        self.opener = opener or build_opener(NoRedirect())

    @staticmethod
    def now():
        return datetime.now(timezone.utc)

    def send(self, message: dict, event_id: str):
        # Stable sequence_id lets supported ntfy clients replace an ambiguous retry.
        payload = {**message, "topic": self.topic, "sequence_id": event_id[:64]}
        headers = {"Content-Type": "application/json", "User-Agent": "launch-alert/1.0"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(self.server + "/", data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if not 200 <= response.status < 300:
                    raise DeliveryError("ntfy returned non-success status")
                data = response.read(65537)
                if len(data) > 65536:
                    raise DeliveryError("ntfy response too large")
                acknowledgment = json.loads(data)
                if not isinstance(acknowledgment, dict) or acknowledgment.get("event") != "message" or not isinstance(acknowledgment.get("id"), str) or not acknowledgment["id"] or acknowledgment.get("topic") != self.topic:
                    raise DeliveryError("ntfy did not acknowledge the message")
        except HTTPError as exc:
            if exc.code in (429, 503):
                raise DeliveryRateLimited(retry_seconds(exc.headers.get("Retry-After"), self.now())) from None
            raise DeliveryError(f"ntfy HTTP {exc.code}") from None
        except (URLError, socket.timeout, TimeoutError, OSError, HTTPException):
            raise DeliveryError("ntfy connection failed") from None
        except (ValueError, UnicodeError):
            raise DeliveryError("ntfy response was not valid JSON") from None
