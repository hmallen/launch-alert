import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from http.client import IncompleteRead
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit

from launch_alert.provider import LaunchLibraryProvider, ProviderError, RateLimited


NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)


def launch(identifier="launch-1"):
    return {
        "id": identifier,
        "name": "Falcon 9 | Example",
        "net": "2026-10-16T12:30:00Z",
        "status": {"id": 1, "name": "Go for Launch", "abbrev": "Go"},
        "last_updated": "2026-10-16T11:59:00Z",
        "net_precision": {"id": 0, "name": "Second"},
        "vid_urls": [{"url": "https://example.com/live", "priority": 1, "live": False}],
    }


class Response(io.BytesIO):
    status = 200

    def __init__(self, data):
        super().__init__(json.dumps(data).encode())


class QueueOpener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append((request, timeout))
        next_response = self.responses.pop(0)
        if isinstance(next_response, BaseException):
            raise next_response
        return Response(next_response)


class ProviderTests(unittest.TestCase):
    def first_url(self):
        query = {
            "net__gte": (NOW - timedelta(hours=2)).isoformat(),
            "net__lte": (NOW + timedelta(hours=48)).isoformat(),
            "mode": "detailed", "ordering": "net", "limit": "100",
        }
        return "https://ll.thespacedevs.com/2.3.0/launches/?" + urlencode(query)

    def test_complete_snapshot_keeps_future_stream_and_recent_outcome(self):
        second_url = self.first_url() + "&offset=1"
        first, second = launch(), launch("launch-2")
        second["status"] = {"id": 3}
        opener = QueueOpener(
            {"count": 2, "next": second_url, "results": [first]},
            {"count": 2, "next": None, "results": [second]},
        )
        provider = LaunchLibraryProvider(opener=opener, api_token="private-token")
        self.assertEqual(provider.fetch(NOW), [first, second])
        self.assertEqual(provider.last_request_count, 2)
        request, timeout = opener.calls[0]
        self.assertEqual(request.get_header("Authorization"), "Token private-token")
        self.assertEqual(timeout, 20)
        self.assertEqual(urlsplit(request.full_url).path, "/2.3.0/launches/")
        self.assertEqual(parse_qs(urlsplit(request.full_url).query)["mode"], ["detailed"])

    def test_before_request_reserves_each_page_before_network(self):
        opener = QueueOpener(
            {"count": 2, "next": self.first_url() + "&offset=1", "results": [launch()]},
            {"count": 2, "next": None, "results": [launch("second")]},
        )
        reservations = []
        provider = LaunchLibraryProvider(
            opener=opener,
            before_request=lambda: reservations.append((len(opener.calls), provider.last_request_count)),
        )
        provider.fetch(NOW)
        self.assertEqual(reservations, [(0, 0), (1, 1)])

    def test_before_request_failure_prevents_io_and_preserves_error(self):
        opener = QueueOpener()
        error = RateLimited("Persisted rate budget exhausted", 180)

        def no_quota():
            raise error

        provider = LaunchLibraryProvider(opener=opener, before_request=no_quota)
        with self.assertRaises(RateLimited) as raised:
            provider.fetch(NOW)
        self.assertIs(raised.exception, error)
        self.assertEqual(opener.calls, [])
        self.assertEqual(provider.last_request_count, 0)

    def test_empty_window_is_valid(self):
        provider = LaunchLibraryProvider(opener=QueueOpener({"count": 0, "next": None, "results": []}))
        self.assertEqual(provider.fetch(NOW), [])

    def test_pagination_cannot_expose_token_or_change_filters(self):
        for url in (
            "https://attacker.example/2.3.0/launches/?offset=100",
            self.first_url().replace("mode=detailed", "mode=list") + "&offset=100",
            self.first_url().replace("/launches/", "/events/") + "&offset=100",
            self.first_url().replace("https://", "http://") + "&offset=100",
        ):
            with self.subTest(url=url):
                opener = QueueOpener({"count": 2, "next": url, "results": [launch()]})
                with self.assertRaises(ProviderError):
                    LaunchLibraryProvider(opener=opener).fetch(NOW)
                self.assertEqual(len(opener.calls), 1)

    def test_incomplete_or_inconsistent_pages_fail_closed(self):
        bad_pages = [
            {"count": 2, "next": None, "results": [launch()]},
            {"count": 0, "next": None, "results": [launch()]},
            {"count": True, "next": None, "results": []},
            {"count": 0, "results": []},
            {"count": 2, "next": self.first_url(), "results": []},
            {"count": 1, "next": 42, "results": [launch()]},
        ]
        for page in bad_pages:
            with self.subTest(page=page):
                with self.assertRaises(ProviderError):
                    LaunchLibraryProvider(opener=QueueOpener(page)).fetch(NOW)

    def test_duplicate_launch_or_changing_count_rejects_all_pages(self):
        for second_page in (
            {"count": 2, "next": None, "results": [launch()]},
            {"count": 3, "next": None, "results": [launch("other")]},
        ):
            opener = QueueOpener(
                {"count": 2, "next": self.first_url() + "&offset=1", "results": [launch()]},
                second_page,
            )
            with self.assertRaises(ProviderError):
                LaunchLibraryProvider(opener=opener).fetch(NOW)

    def test_pagination_loop_is_bounded(self):
        opener = QueueOpener({"count": 2, "next": self.first_url(), "results": [launch()]})
        with self.assertRaises(ProviderError):
            LaunchLibraryProvider(opener=opener).fetch(NOW)
        self.assertEqual(len(opener.calls), 1)

    def test_missing_detail_or_invalid_launch_rejects_snapshot(self):
        for key, value in [("vid_urls", None), ("net", "today"), ("net", "2026-10-16T12:30:00"), ("status", {"id": "1"})]:
            with self.subTest(key=key, value=value):
                record = launch()
                record[key] = value
                with self.assertRaises(ProviderError):
                    LaunchLibraryProvider(opener=QueueOpener({"count": 1, "next": None, "results": [record]})).fetch(NOW)

    def test_rate_limit_honors_seconds_or_http_date(self):
        for value, seconds in [("900", 900), ("Fri, 16 Oct 2026 12:10:00 GMT", 600), ("invalid", 300)]:
            with self.subTest(value=value):
                error = HTTPError(self.first_url(), 429, "Too Many Requests", {"Retry-After": value}, io.BytesIO())
                provider = LaunchLibraryProvider(opener=QueueOpener(error))
                with self.assertRaises(RateLimited) as raised:
                    provider.fetch(NOW)
                self.assertEqual(raised.exception.retry_after, seconds)
                self.assertEqual(provider.last_request_count, 1)

    def test_network_error_never_returns_partial_results(self):
        opener = QueueOpener(
            {"count": 2, "next": self.first_url() + "&offset=1", "results": [launch()]},
            URLError("offline"),
        )
        provider = LaunchLibraryProvider(opener=opener)
        with self.assertRaises(ProviderError):
            provider.fetch(NOW)
        self.assertEqual(provider.last_request_count, 2)

    def test_redirect_http_error_fails_without_revealing_credentials(self):
        error = HTTPError(self.first_url(), 302, "Moved", {"Location": "https://attacker.example"}, io.BytesIO())
        opener = QueueOpener(error)
        with self.assertRaisesRegex(ProviderError, "HTTP 302"):
            LaunchLibraryProvider(opener=opener, api_token="private-token").fetch(NOW)
        self.assertEqual(len(opener.calls), 1)

    def test_truncated_http_response_is_retryable_provider_failure(self):
        opener = QueueOpener(IncompleteRead(b"partial response", 100))
        with self.assertRaises(ProviderError):
            LaunchLibraryProvider(opener=opener).fetch(NOW)

    def test_temporary_server_error_honors_retry_after(self):
        error = HTTPError(self.first_url(), 503, "Unavailable", {"Retry-After": "600"}, io.BytesIO())
        with self.assertRaises(ProviderError) as raised:
            LaunchLibraryProvider(opener=QueueOpener(error)).fetch(NOW)
        self.assertEqual(raised.exception.retry_after, 600)

    def test_invalid_configuration(self):
        for url in ["http://example.com/2.3.0", "https://user:pass@example.com", "https://example.com/?token=secret"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                LaunchLibraryProvider(base_url=url)
        with self.assertRaises(ValueError):
            LaunchLibraryProvider().fetch(datetime(2026, 10, 16))


if __name__ == "__main__":
    unittest.main()
