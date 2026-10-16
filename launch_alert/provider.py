"""Bounded, complete Launch Library 2 snapshots using only the standard library.

The production API permits 15 unauthenticated requests per hour per IP. Each
page is a request; the service owns the polling schedule and backoff. A snapshot
is returned only after all pages have been read and validated. No stale cache is
silently substituted after a failure.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


DEFAULT_BASE_URL = "https://ll.thespacedevs.com/2.3.0"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_PAGES = 20


class ProviderError(RuntimeError):
    """The source did not supply a usable complete snapshot."""

    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class RateLimited(ProviderError):
    """The source asked us to stop making requests temporarily."""


class _NoRedirect(HTTPRedirectHandler):
    # urllib forwards Authorization on redirects. Require an explicit endpoint
    # update rather than sending the configured API key to a redirect target.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _retry_after(value: str | None, now: datetime) -> int | None:
    if not value:
        return None
    try:
        delay = float(value)
        if math.isfinite(delay) and delay >= 0:
            return max(1, math.ceil(delay))
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(1, math.ceil((when - now).total_seconds()))
    except (TypeError, ValueError, OverflowError):
        return None


class LaunchLibraryProvider:
    """Fetch a recent/upcoming launch window, including detailed webcast URLs.

    ``opener`` is an optional urllib-compatible callable for deterministic tests.
    HTTP is allowed only for an explicitly configured loopback endpoint, useful
    for local integration tests. All production endpoints require HTTPS.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        api_token: str | None = None,
        timeout_seconds: float = 20,
        opener: Callable[..., Any] | None = None,
        before_request: Callable[[], None] | None = None,
    ):
        parsed = urlsplit(base_url)
        local_http = parsed.scheme == "http" and parsed.hostname in {
            "localhost", "127.0.0.1", "::1"
        }
        if (
            (parsed.scheme != "https" and not local_http)
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Launch Library URL must be an HTTPS API base URL")
        # Accessing port also rejects malformed authority values early.
        parsed.port
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("Provider timeout must be a positive finite number")
        if api_token and ("\r" in api_token or "\n" in api_token):
            raise ValueError("API token cannot contain newlines")
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.timeout_seconds = timeout_seconds
        self._open = opener or build_opener(_NoRedirect()).open
        self.before_request = before_request
        self.last_request_count = 0

    def fetch(self, now: datetime, horizon_hours: float = 48) -> list[dict]:
        """Return all launches from two hours ago through the configured horizon.

        Recent records allow the engine to observe holds and launch outcomes.
        Missing launches mean only that they left this window, not that they were
        cancelled. A page failure raises and returns no partial snapshot.
        """
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Provider fetch time must include a timezone")
        if not math.isfinite(horizon_hours) or horizon_hours <= 0:
            raise ValueError("Provider horizon must be a positive finite number")
        now = now.astimezone(timezone.utc)
        endpoint = self.base_url + "/launches/"
        fixed = {
            "net__gte": (now - timedelta(hours=2)).isoformat(),
            "net__lte": (now + timedelta(hours=horizon_hours)).isoformat(),
            "mode": "detailed",
            "ordering": "net",
            "limit": "100",
        }
        url: str | None = endpoint + "?" + urlencode(fixed)
        seen_urls: set[str] = set()
        seen_ids: set[str] = set()
        launches: list[dict] = []
        expected_count: int | None = None
        self.last_request_count = 0
        while url:
            if url in seen_urls or len(seen_urls) >= MAX_PAGES:
                raise ProviderError("Launch Library pagination loop or page limit")
            self._validate_page_url(url, endpoint, fixed)
            seen_urls.add(url)
            page = self._request_json(url, now)
            if not isinstance(page, dict):
                raise ProviderError("Launch Library response is not an object")
            count = page.get("count")
            results = page.get("results")
            if type(count) is not int or count < 0 or not isinstance(results, list):
                raise ProviderError("Launch Library page has invalid count/results")
            if count > MAX_PAGES * 100:
                raise ProviderError("Launch Library snapshot exceeds page limit")
            if expected_count is None:
                expected_count = count
            elif count != expected_count:
                raise ProviderError("Launch Library data changed while paginating")
            for launch in results:
                self._validate_launch(launch)
                launch_id = launch["id"]
                if launch_id in seen_ids:
                    raise ProviderError("Launch Library pages contain duplicate launch IDs")
                seen_ids.add(launch_id)
                launches.append(launch)
            if len(launches) > count:
                raise ProviderError("Launch Library results exceed advertised count")
            if "next" not in page:
                raise ProviderError("Launch Library page has no pagination marker")
            next_url = page["next"]
            if next_url is not None and not isinstance(next_url, str):
                raise ProviderError("Launch Library page has invalid next URL")
            if next_url and not results:
                raise ProviderError("Launch Library returned an empty non-final page")
            url = urljoin(endpoint, next_url) if next_url else None
        if len(launches) != expected_count:
            raise ProviderError("Launch Library returned an incomplete snapshot")
        return launches

    @staticmethod
    def _validate_page_url(url: str, endpoint: str, fixed: dict[str, str]) -> None:
        parsed, expected = urlsplit(url), urlsplit(endpoint)
        if (
            parsed.scheme != expected.scheme
            or parsed.netloc != expected.netloc
            or parsed.path != expected.path
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ProviderError("Launch Library pagination left the configured endpoint")
        query = parse_qs(parsed.query)
        if any(query.get(key) != [value] for key, value in fixed.items()):
            raise ProviderError("Launch Library pagination changed snapshot filters")

    @staticmethod
    def _validate_launch(launch: Any) -> None:
        if (
            not isinstance(launch, dict)
            or not isinstance(launch.get("id"), str)
            or not launch["id"]
            or not isinstance(launch.get("net"), str)
            or not isinstance(launch.get("status"), dict)
            or type(launch["status"].get("id")) is not int
            or not isinstance(launch.get("vid_urls"), list)
        ):
            raise ProviderError("Launch Library returned an invalid detailed launch record")
        try:
            net = datetime.fromisoformat(launch["net"].replace("Z", "+00:00"))
            if net.tzinfo is None or net.utcoffset() is None:
                raise ValueError("NET has no timezone")
        except ValueError as error:
            raise ProviderError("Launch Library returned an invalid launch timestamp") from error

    def _request_json(self, url: str, now: datetime) -> Any:
        headers = {
            "Accept": "application/json",
            "User-Agent": "launch-alert/1.0 (personal launch reminders)",
        }
        if self.api_token:
            headers["Authorization"] = "Token " + self.api_token
        request = Request(url, headers=headers)
        # Reserve quota durably before attempting I/O, including every page.
        # The owner may raise RateLimited here without any request being sent.
        if self.before_request is not None:
            self.before_request()
        self.last_request_count += 1
        try:
            with self._open(request, timeout=self.timeout_seconds) as response:
                if response.status != 200:
                    raise ProviderError(f"Launch Library returned HTTP {response.status}")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ProviderError("Launch Library response is too large")
                return json.loads(raw)
        except HTTPError as error:
            retry = _retry_after(error.headers.get("Retry-After"), now)
            error.close()
            if error.code == 429:
                raise RateLimited("Launch Library rate limit reached", retry or 300) from error
            raise ProviderError(f"Launch Library returned HTTP {error.code}", retry) from error
        except (URLError, OSError, TimeoutError, HTTPException) as error:
            raise ProviderError("Launch Library request failed; check network access") from error
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ProviderError("Launch Library response is not valid JSON") from error
