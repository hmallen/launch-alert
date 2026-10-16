import io
import json
import unittest
from datetime import datetime, timezone
from http.client import IncompleteRead
from urllib.error import HTTPError, URLError
from urllib.request import HTTPHandler, build_opener
from urllib.response import addinfourl

from launch_alert.notifier import (
    DeliveryError,
    DeliveryRateLimited,
    NoRedirect,
    NtfyNotifier,
)


NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
TOPIC = "private-personal-launch-topic"
TOKEN = "private-token-never-log"


class Response(io.BytesIO):
    def __init__(self, body, status=200):
        super().__init__(body)
        self.status = status


class RecordingOpener:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def acknowledgment(**overrides):
    body = {"event": "message", "id": "accepted-message-id", "topic": TOPIC}
    body.update(overrides)
    return json.dumps(body).encode()


class RedirectingTransport(HTTPHandler):
    """Exercise urllib's real redirect processing without opening any sockets."""

    def __init__(self):
        super().__init__()
        self.requests = []

    def http_open(self, request):
        self.requests.append(request)
        response = addinfourl(
            io.BytesIO(),
            {"Location": "http://untrusted-target.example/"},
            request.full_url,
            code=302,
        )
        response.msg = "Found"
        return response


class NotifierTests(unittest.TestCase):
    def notifier(self, response):
        opener = RecordingOpener(response)
        notifier = NtfyNotifier("https://ntfy.sh", TOPIC, token=TOKEN, opener=opener)
        notifier.now = lambda: NOW
        return notifier, opener

    def test_json_publish_root_auth_click_action_and_stable_sequence(self):
        notifier, opener = self.notifier(Response(acknowledgment()))
        message = {
            "title": "Launch in 30 minutes",
            "message": "Rocket | Mission — 12:30 UTC",
            "priority": 4,
            "tags": ["rocket"],
            "click": "https://example.com/livestream",
            "actions": [{"action": "view", "label": "Watch", "url": "https://example.com/livestream"}],
        }
        event_id = "stable-event-id"
        notifier.send(message, event_id)
        request, timeout = opener.calls[0]
        self.assertEqual(request.full_url, "https://ntfy.sh/")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + TOKEN)
        self.assertEqual(timeout, 15)
        payload = json.loads(request.data)
        self.assertEqual(payload, {**message, "topic": TOPIC, "sequence_id": event_id})
        self.assertNotIn("Cache", request.headers)
        self.assertNotIn("Firebase", request.headers)
        self.assertNotIn("topic", message)
        self.assertNotIn("sequence_id", message)

    def test_payload_topic_and_sequence_are_owned_by_notifier(self):
        notifier, opener = self.notifier(Response(acknowledgment()))
        notifier.send({"message": "test", "topic": "wrong-topic", "sequence_id": "wrong-sequence"}, "a" * 100)
        payload = json.loads(opener.calls[0][0].data)
        self.assertEqual(payload["topic"], TOPIC)
        self.assertEqual(payload["sequence_id"], "a" * 64)

    def test_anonymous_publish_does_not_add_authorization(self):
        opener = RecordingOpener(Response(acknowledgment()))
        NtfyNotifier("https://ntfy.sh", TOPIC, opener=opener).send({"message": "test"}, "event")
        self.assertIsNone(opener.calls[0][0].get_header("Authorization"))

    def test_invalid_acknowledgment_never_reports_delivery_success(self):
        bad_bodies = [
            b"not-json",
            b"\xff",
            b"[]",
            b"null",
            acknowledgment(event="keepalive"),
            acknowledgment(id=""),
            acknowledgment(id=True),
            acknowledgment(id={"unexpected": "object"}),
            acknowledgment(topic="different-topic"),
            json.dumps({"event": "message", "id": "id"}).encode(),
            b"x" * 65537,
        ]
        for body in bad_bodies:
            with self.subTest(body=body[:80]):
                notifier, _ = self.notifier(Response(body))
                with self.assertRaises(DeliveryError):
                    notifier.send({"message": "test"}, "event")

    def test_http_status_is_checked_even_with_valid_acknowledgment(self):
        notifier, _ = self.notifier(Response(acknowledgment(), status=500))
        with self.assertRaises(DeliveryError):
            notifier.send({"message": "test"}, "event")

    def test_429_and_503_retry_after_seconds_http_date_and_fallback(self):
        for status in (429, 503):
            for value, expected in (
                ("600", 600),
                ("Fri, 16 Oct 2026 12:10:00 GMT", 600),
                ("Fri, 16 Oct 2026 11:00:00 GMT", 1),
                ("invalid", 300),
                (None, 300),
            ):
                with self.subTest(status=status, retry_after=value):
                    headers = {"Retry-After": value} if value is not None else {}
                    error = HTTPError("https://ntfy.sh/" + TOPIC, status, "Error", headers, io.BytesIO())
                    notifier, _ = self.notifier(error)
                    with self.assertRaises(DeliveryRateLimited) as raised:
                        notifier.send({"message": "test"}, "event")
                    self.assertEqual(raised.exception.retry_after, expected)

    def test_transport_and_http_errors_do_not_expose_topic_token_or_body(self):
        private_url = "https://ntfy.sh/" + TOPIC + "?token=" + TOKEN
        errors = [
            URLError("cannot connect to " + private_url),
            TimeoutError("timeout " + private_url),
            OSError("failed " + private_url),
            HTTPError(private_url, 401, "unauthorized " + TOKEN, {}, io.BytesIO(TOPIC.encode())),
        ]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                notifier, _ = self.notifier(error)
                with self.assertRaises(DeliveryError) as raised:
                    notifier.send({"message": "test"}, "event")
                self.assertNotIn(TOPIC, str(raised.exception))
                self.assertNotIn(TOKEN, str(raised.exception))
                self.assertTrue(raised.exception.__suppress_context__)

    def test_redirect_cannot_forward_authenticated_publish(self):
        transport = RedirectingTransport()
        opener = build_opener(transport, NoRedirect())
        notifier = NtfyNotifier("http://source.example", TOPIC, token=TOKEN, opener=opener)
        with self.assertRaisesRegex(DeliveryError, "HTTP 302"):
            notifier.send({"message": "test"}, "event")
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(transport.requests[0].full_url, "http://source.example/")

    def test_truncated_response_becomes_delivery_failure(self):
        notifier, _ = self.notifier(IncompleteRead(b"partial", 100))
        with self.assertRaises(DeliveryError):
            notifier.send({"message": "test"}, "event")


if __name__ == "__main__":
    unittest.main()
