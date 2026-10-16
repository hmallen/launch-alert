"""Normalize source records and define conservative reminder eligibility."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

UTC = timezone.utc


def timestamp(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(UTC) if result.tzinfo is not None else None
    except ValueError:
        return None


@dataclass(frozen=True)
class Launch:
    id: str
    name: str
    net: str | None
    status: int | None
    status_name: str
    stream: str | None
    precise: bool

    @property
    def time(self) -> datetime | None:
        return timestamp(self.net)

    @property
    def eligible(self) -> bool:
        # Go and TBC are prelaunch; TBD, hold, deployed and terminal states aren't.
        return self.status in (1, 8) and self.precise and self.time is not None and self.stream is not None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_source(cls, raw: dict) -> "Launch":
        launch_id = raw.get("id")
        name = raw.get("name")
        if not isinstance(launch_id, str) or not launch_id or not isinstance(name, str) or not name.strip():
            raise ValueError("Launch record lacks a stable ID or name")
        status = raw.get("status") or {}
        if not isinstance(status, dict):
            status = {}
        precision = raw.get("net_precision")
        precision_name = precision.get("name") if isinstance(precision, dict) else None
        precise = isinstance(precision_name, str) and precision_name.lower() in ("second", "minute")
        streams = raw.get("vid_urls", raw.get("vidURLs", [])) or []
        valid = []
        if isinstance(streams, list):
            for item in streams:
                if not isinstance(item, dict) or not isinstance(item.get("url"), str):
                    continue
                try:
                    url = urlsplit(item["url"])
                except ValueError:
                    continue
                if url.scheme == "https" and url.hostname and not url.username and not url.password:
                    priority = item.get("priority")
                    valid.append((priority if isinstance(priority, (int, float)) else 100, item["url"]))
        valid.sort()
        net = timestamp(raw.get("net"))
        status_id = status.get("id")
        return cls(launch_id, name.strip(), net.isoformat() if net else None,
                   status_id if type(status_id) is int else None,
                   str(status.get("name") or "Unknown"), valid[0][1] if valid else None, precise)
