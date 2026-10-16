"""Schedule reconciliation and at-least-once delivery through a durable outbox."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .model import Launch, timestamp
from .storage import Store


class Engine:
    def __init__(self, store: Store, lead_minutes: int = 30, max_data_age: int = 900):
        self.store = store
        self.lead = timedelta(minutes=lead_minutes)
        self.max_data_age = max_data_age

    def reconcile(self, records: list[dict], now: datetime):
        # Normalize the entire snapshot first; one malformed row cannot partially replace state.
        launches = [Launch.from_source(raw) for raw in records]
        if len({item.id for item in launches}) != len(launches):
            raise ValueError("Duplicate launch IDs in snapshot")
        db = self.store.db
        with db:
            previous_rows = {row["id"]: row for row in db.execute("SELECT * FROM launches").fetchall()}
            db.execute("UPDATE launches SET present=0")
            for launch in launches:
                row = previous_rows.get(launch.id)
                previous = Launch(**json.loads(row["payload"])) if row else None
                # A launch that returns to the snapshot invalidates a pending withdrawal.
                db.execute("UPDATE events SET canceled_at=? WHERE launch_id=? AND kind='withdrawn' AND delivered_at IS NULL AND canceled_at IS NULL",
                           (now.isoformat(), launch.id))
                was_missing = row is not None and not row["present"]
                changed = previous != launch or was_missing
                revision = (row["revision"] + int(changed)) if row else 1
                payload = json.dumps(launch.to_dict())
                db.execute("""INSERT INTO launches VALUES (?,?,?,?,1) ON CONFLICT(id) DO UPDATE SET
                    payload=excluded.payload,revision=excluded.revision,seen_at=excluded.seen_at,present=1""",
                           (launch.id, payload, revision, now.isoformat()))
                if changed:
                    db.execute("UPDATE events SET canceled_at=? WHERE launch_id=? AND delivered_at IS NULL AND canceled_at IS NULL",
                               (now.isoformat(), launch.id))
                    # Correct an already delivered reminder when the source withdraws it or moves NET.
                    sent = db.execute("SELECT 1 FROM events WHERE launch_id=? AND kind='reminder' AND delivered_at IS NOT NULL LIMIT 1", (launch.id,)).fetchone()
                    latest_sent = db.execute("SELECT kind,payload FROM events WHERE launch_id=? AND delivered_at IS NOT NULL ORDER BY delivered_at DESC,rowid DESC LIMIT 1", (launch.id,)).fetchone()
                    needs_restoration = latest_sent and (latest_sent["kind"] == "withdrawn" or "no longer confirmed" in (json.loads(latest_sent["payload"])["reason"] or ""))
                    if previous and sent and previous.time and now < previous.time:
                        reason = None
                        if not launch.eligible:
                            reason = "The earlier launch reminder is no longer confirmed"
                        elif needs_restoration and (not previous.eligible or was_missing) and (launch.net == previous.net or launch.time > now + self.lead):
                            reason = "Launch is back on the schedule"
                        elif launch.net != previous.net and launch.time > now + self.lead:
                            reason = "Launch time changed"
                        elif launch.stream != previous.stream and (launch.net == previous.net or launch.time > now + self.lead):
                            reason = "Livestream link changed"
                        if reason:
                            expiry = min(now + timedelta(hours=1), launch.time) if launch.eligible else previous.time
                            if expiry > now:
                                self._queue(f"update:{launch.id}:{revision}", launch, revision, "update", now, expiry, reason)
            # A disappearing launch may have moved beyond the query window. Withdraw its reminder.
            missing = db.execute("SELECT * FROM launches WHERE present=0").fetchall()
            for row in missing:
                db.execute("UPDATE events SET canceled_at=? WHERE launch_id=? AND delivered_at IS NULL AND canceled_at IS NULL", (now.isoformat(), row["id"]))
                launch = Launch(**json.loads(row["payload"]))
                sent = db.execute("SELECT 1 FROM events WHERE launch_id=? AND kind='reminder' AND delivered_at IS NOT NULL LIMIT 1", (launch.id,)).fetchone()
                if sent and launch.time and now < launch.time:
                    self._queue(f"missing:{launch.id}:{row['revision']}", launch, row["revision"], "withdrawn", now, launch.time,
                                "Launch is no longer in the current schedule; the earlier reminder is no longer confirmed")
            self.store.set("last_fetch", now.isoformat())
            self.store.set("source_error", None)
        self.schedule(now)

    def _queue(self, key, launch, revision, kind, now, expires, reason=None):
        payload = json.dumps({"launch": launch.to_dict(), "reason": reason})
        # Previously canceled, unsent events may become eligible again (e.g. hold lifted).
        self.store.db.execute("""INSERT INTO events
            (key,launch_id,revision,kind,payload,expires_at,created_at,next_try)
            VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
            revision=excluded.revision,payload=excluded.payload,expires_at=excluded.expires_at,canceled_at=NULL,
            next_try=CASE WHEN events.canceled_at IS NOT NULL THEN excluded.next_try ELSE events.next_try END
            WHERE events.delivered_at IS NULL""",
            (hashlib.sha256(key.encode()).hexdigest(), launch.id, revision, kind, payload,
             expires.isoformat(), now.isoformat(), now.isoformat()))

    def fresh(self, now: datetime) -> bool:
        last = timestamp(self.store.get("last_fetch"))
        return last is not None and 0 <= (now - last).total_seconds() <= self.max_data_age

    def schedule(self, now: datetime):
        if not self.fresh(now):
            return
        with self.store.db:
            self.store.db.execute("UPDATE events SET canceled_at=? WHERE expires_at<=? AND delivered_at IS NULL AND canceled_at IS NULL", (now.isoformat(), now.isoformat()))
            for row in self.store.db.execute("SELECT * FROM launches WHERE present=1").fetchall():
                launch = Launch(**json.loads(row["payload"]))
                if launch.eligible and now < launch.time <= now + self.lead:
                    self._queue(f"reminder:{launch.id}:{launch.net}", launch, row["revision"], "reminder", now, launch.time)

    def deliver(self, notifier, now: datetime, tz: ZoneInfo):
        if not self.fresh(now):
            return
        cooldown = timestamp(self.store.get("delivery_retry_until"))
        if cooldown and now < cooldown:
            return
        db = self.store.db
        rows = db.execute("SELECT * FROM events WHERE delivered_at IS NULL AND canceled_at IS NULL AND next_try<=? ORDER BY created_at,key", (now.isoformat(),)).fetchall()
        for row in rows:
            # Re-evaluate time before every network request; a slow request must not send later events after NET.
            sent_at = max(now, notifier.now()) if hasattr(notifier, "now") else now
            if not self.fresh(sent_at):
                break
            if timestamp(row["expires_at"]) <= sent_at:
                with db:
                    db.execute("UPDATE events SET canceled_at=? WHERE key=?", (sent_at.isoformat(), row["key"]))
                continue
            payload = json.loads(row["payload"])
            launch = Launch(**payload["launch"])
            message = notification(launch, row["kind"], payload["reason"], sent_at, tz)
            try:
                notifier.send(message, row["key"])
            except Exception as exc:
                failed_at = max(sent_at, notifier.now()) if hasattr(notifier, "now") else sent_at
                # The notifier exposes only safe error summaries; never persist tokens/URLs from exceptions.
                delay = min(300, 10 * 2 ** min(row["attempts"], 5))
                delay = max(delay, getattr(exc, "retry_after", 0) or 0)
                with db:
                    db.execute("UPDATE events SET attempts=attempts+1,next_try=?,error=? WHERE key=?",
                               ((failed_at + timedelta(seconds=delay)).isoformat(), type(exc).__name__, row["key"]))
                # Respect provider-wide cooldown (including Retry-After), not just this message.
                if getattr(exc, "retry_after", 0):
                    with db:
                        self.store.set("delivery_retry_until", (failed_at + timedelta(seconds=delay)).isoformat())
                        db.execute("UPDATE events SET next_try=MAX(next_try,?) WHERE delivered_at IS NULL AND canceled_at IS NULL",
                                   ((failed_at + timedelta(seconds=delay)).isoformat(),))
                break
            else:
                accepted_at = max(sent_at, notifier.now()) if hasattr(notifier, "now") else sent_at
                with db:
                    db.execute("UPDATE events SET delivered_at=?,attempts=attempts+1,error=NULL WHERE key=?", (accepted_at.isoformat(), row["key"]))
                    self.store.set("last_delivery", accepted_at.isoformat())


def notification(launch: Launch, kind: str, reason: str | None, now: datetime, tz: ZoneInfo) -> dict:
    launch_time = launch.time
    local = launch_time.astimezone(tz).strftime("%a %b %d, %I:%M %p %Z") if launch_time else "time TBD"
    if kind == "reminder":
        minutes = max(1, math.ceil((launch_time - now).total_seconds() / 60))
        title = f"Launch in {minutes} min: {launch.name}"
        text = f"{launch.name}\nScheduled for {local}. Status: {launch.status_name}.\nWatch: {launch.stream}\nLaunch times can change."
    else:
        title = f"Launch update: {launch.name}"
        text = f"{reason}.\n{launch.name}\nLatest NET: {local}. Status: {launch.status_name}."
        if kind != "withdrawn" and launch.stream:
            text += f"\nWatch: {launch.stream}"
    result = {"title": title[:240], "message": text, "priority": 4 if kind == "reminder" else 3, "tags": ["rocket"]}
    if launch.stream and kind != "withdrawn":
        result["click"] = launch.stream
        result["actions"] = [{"action": "view", "label": "Watch livestream", "url": launch.stream}]
    return result
