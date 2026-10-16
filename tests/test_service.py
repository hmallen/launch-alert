"""Cross-component recovery and source-budget tests, without external services."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from launch_alert.provider import ProviderError, RateLimited
from launch_alert.service import Worker, report
from launch_alert.storage import Store, worker_lock


NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)


def launch(net=None):
    return {"id": "one", "name": "A launch", "net": (net or NOW + timedelta(minutes=30)).isoformat(),
            "status": {"id": 1, "name": "Go for Launch"}, "net_precision": {"name": "Second"},
            "vid_urls": [{"url": "https://example.com/live", "priority": 1}]}


class FakeProvider:
    def __init__(self, result, count=1):
        self.result = result
        self.last_request_count = count
        self.calls = 0

    def fetch(self, now):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeNotifier:
    def __init__(self):
        self.messages = []

    def send(self, message, event_id):
        self.messages.append(message)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.store = Store(self.path)
        self.config = SimpleNamespace(lead_minutes=30, max_data_age=900, poll_seconds=300,
                                     ll2_token=None, timezone=ZoneInfo("America/Chicago"))
        self.sender = FakeNotifier()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def worker(self, provider):
        return Worker(self.config, self.store, provider, self.sender)

    def test_due_reminder_survives_restart_without_extra_fetch_or_duplicate(self):
        provider = FakeProvider([launch()])
        self.worker(provider).tick(NOW)
        self.assertEqual(len(self.sender.messages), 1)
        self.store.close()
        self.store = Store(self.path)
        self.worker(provider).tick(NOW + timedelta(seconds=10))
        self.assertEqual(provider.calls, 1)
        self.assertEqual(len(self.sender.messages), 1)
        self.assertTrue(report(self.store, NOW)["healthy"])

    def test_failed_refresh_preserves_recent_snapshot_then_stale_data_stops_alerts(self):
        first = launch(NOW + timedelta(minutes=36))
        worker = self.worker(FakeProvider([first]))
        worker.tick(NOW)
        worker.provider = FakeProvider(ProviderError("offline"))
        worker.tick(NOW + timedelta(minutes=5))
        self.assertEqual(self.store.get("last_fetch"), NOW.isoformat())
        worker.tick(NOW + timedelta(minutes=6))
        self.assertEqual(len(self.sender.messages), 1)
        self.assertEqual(self.store.get("source_error"), "ProviderError")
        self.assertFalse(report(self.store, NOW + timedelta(minutes=16))["healthy"])

    def test_partial_bad_snapshot_does_not_advance_success_or_replace_state(self):
        worker = self.worker(FakeProvider([launch(NOW + timedelta(hours=2))]))
        worker.tick(NOW)
        worker.provider = FakeProvider([launch(), {"id": "bad"}])
        worker.tick(NOW + timedelta(minutes=5))
        row = self.store.db.execute("SELECT payload FROM launches").fetchone()
        self.assertEqual(json.loads(row[0])["net"], (NOW + timedelta(hours=2)).isoformat())
        self.assertEqual(self.store.get("last_fetch"), NOW.isoformat())
        self.assertEqual(len(self.sender.messages), 0)

    def test_source_retry_after_and_page_count_are_durable(self):
        provider = FakeProvider(RateLimited("wait", 1200), count=2)
        self.worker(provider).tick(NOW)
        self.assertEqual(self.store.get("next_fetch"), (NOW + timedelta(seconds=1200)).isoformat())
        self.store.close()
        self.store = Store(self.path)
        self.worker(provider).tick(NOW + timedelta(minutes=5))
        self.assertEqual(provider.calls, 1)

    def test_paginated_snapshot_scales_polling_budget(self):
        worker = self.worker(FakeProvider([], count=3))
        worker.tick(NOW)
        self.assertEqual(self.store.get("next_fetch"), (NOW + timedelta(seconds=900)).isoformat())

    def test_sliding_request_budget_is_reserved_before_io_and_survives_restart(self):
        worker = self.worker(FakeProvider([]))
        with patch("launch_alert.service.utcnow", return_value=NOW):
            for _ in range(15):
                worker.reserve_source_request()
            with self.assertRaises(RateLimited):
                worker.reserve_source_request()
        self.store.close()
        self.store = Store(self.path)
        worker = self.worker(FakeProvider([]))
        with patch("launch_alert.service.utcnow", return_value=NOW + timedelta(minutes=30)):
            with self.assertRaises(RateLimited) as error:
                worker.reserve_source_request()
            self.assertGreaterEqual(error.exception.retry_after, 1800)
        with patch("launch_alert.service.utcnow", return_value=NOW + timedelta(hours=1, seconds=1)):
            worker.reserve_source_request()
        self.assertEqual(len(self.store.get("source_requests")), 1)

    def test_slow_source_refresh_cannot_send_expired_reminder(self):
        worker = self.worker(FakeProvider([launch(NOW + timedelta(seconds=10))]))
        with patch("launch_alert.service.utcnow", side_effect=[NOW, NOW + timedelta(seconds=20)]):
            worker.tick()
        self.assertEqual(self.sender.messages, [])

    def test_second_worker_is_rejected_before_it_can_send(self):
        with worker_lock(self.path):
            with self.assertRaisesRegex(RuntimeError, "Another worker"):
                with worker_lock(self.path):
                    self.fail("acquired lock twice")

    def test_replaced_stream_updates_an_already_delivered_reminder(self):
        source = launch()
        worker = self.worker(FakeProvider([source]))
        worker.tick(NOW)
        source["vid_urls"] = [{"url": "https://example.com/replacement-stream", "priority": 1}]
        worker.tick(NOW + timedelta(minutes=5))
        self.assertEqual(len(self.sender.messages), 2)
        self.assertIn("Livestream link changed", self.sender.messages[-1]["message"])
        self.assertEqual(self.sender.messages[-1]["click"], "https://example.com/replacement-stream")


if __name__ == "__main__":
    unittest.main()
