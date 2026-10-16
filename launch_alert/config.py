"""Small, dependency-free configuration loader."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def load_env(path: Path = Path(".env")) -> None:
    """Read literal KEY=value pairs, without executing shell substitutions."""
    if not path.exists():
        return
    for number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key.strip().replace("_", "a").isalnum():
            raise ValueError(f"Invalid .env assignment on line {number}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def integer(name: str, default: int, minimum: int, maximum: int) -> int:
    value = int(os.environ.get(name, default))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True)
class Config:
    ntfy_server: str
    ntfy_topic: str
    ntfy_token: str | None
    lead_minutes: int
    poll_seconds: int
    db_path: Path
    timezone: ZoneInfo
    health_host: str
    health_port: int
    max_data_age: int
    ll2_base_url: str
    ll2_token: str | None

    @classmethod
    def from_env(cls) -> "Config":
        server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        base_url = os.environ.get("LL2_BASE_URL", "https://ll.thespacedevs.com/2.3.0").rstrip("/")
        for name, value in (("NTFY_SERVER", server), ("LL2_BASE_URL", base_url)):
            parsed = urlsplit(value)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError(f"{name} must be an HTTPS URL without credentials, query, or fragment")
        topic = os.environ.get("NTFY_TOPIC", "")
        if not topic or len(topic) > 200 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in topic):
            raise ValueError("Set NTFY_TOPIC to a private random topic (run: python -m launch_alert init)")
        poll = integer("POLL_SECONDS", 300, 60, 86400)
        max_age = integer("MAX_DATA_AGE_SECONDS", 900, 60, 3600)
        if poll >= max_age:
            raise ValueError("POLL_SECONDS must be shorter than MAX_DATA_AGE_SECONDS")
        try:
            tz = ZoneInfo(os.environ.get("DISPLAY_TIMEZONE") or os.environ.get("TZ") or "America/Chicago")
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("DISPLAY_TIMEZONE must name an installed IANA time zone") from None
        return cls(
            ntfy_server=server,
            ntfy_topic=topic,
            ntfy_token=os.environ.get("NTFY_TOKEN") or None,
            lead_minutes=integer("LEAD_MINUTES", 30, 1, 1440),
            poll_seconds=poll,
            db_path=Path(os.environ.get("DB_PATH", "data/launch-alert.sqlite3")),
            timezone=tz,
            health_host=os.environ.get("HEALTH_HOST", "127.0.0.1"),
            health_port=integer("HEALTH_PORT", 8080, 0, 65535),
            max_data_age=max_age,
            ll2_base_url=base_url,
            ll2_token=os.environ.get("LL2_TOKEN") or None,
        )
