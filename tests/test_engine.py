from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zoneinfo import ZoneInfo

from launch_alert.engine import Engine, notification
from launch_alert.model import Launch, timestamp
from launch_alert.storage import Store


UTC = timezone.utc
NOW = datetime(2030, 10, 16, 12, tzinfo=UTC)
TZ = ZoneInfo("America/Chicago")


def record(net=None, **changes):
    value = {
        "id": "launch-1", "name": "Falcon 9 | Test mission",
        "net": (net or NOW + timedelta(minutes=30)).isoformat(),
        "status": {"id": 1, "name": "Go"},
        "net_precision": {"name": "Second"},
        "vid_urls": [{"url": "https://example.com/watch", "priority": 1}],
    }
    value.update(changes)
    return value


class FakeNotifier:
    """Deterministic sender with no real clock or network access."""

    def __init__(self, failures=()):
        self.messages = []
        self.calls = []
        self.failures = list(failures)

    def send(self, message, key):
        self.calls.append((message, key))
        if self.failures:
            raise self.failures.pop(0)
        self.messages.append((message, key))


class RateLimited(Exception):
    retry_after = 60


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.store = Store(self.path)
        self.engine = Engine(self.store)
        self.sender = FakeNotifier()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def events(self, clause="", params=()):
        return self.store.db.execute("SELECT * FROM events " + clause, params).fetchall()

    def deliver(self, now=NOW):
        self.engine.deliver(self.sender, now, TZ)

    def test_reminder_becomes_due_at_exactly_thirty_minutes(self):
        launch = record(NOW + timedelta(minutes=30, seconds=1))
        self.engine.reconcile([launch], NOW)
        self.deliver()
        self.assertEqual(self.sender.messages, [])
        due = NOW + timedelta(seconds=1)
        self.engine.schedule(due)
        self.deliver(due)
        self.assertEqual(len(self.sender.messages), 1)
        self.assertIn("Launch in 30 min", self.sender.messages[0][0]["title"])

    def test_no_stream_imprecise_hold_and_terminal_launches_never_queue(self):
        for changes in (
            {"vid_urls": []}, {"net_precision": {"name": "Day"}},
            {"status": {"id": 5, "name": "On Hold"}},
            {"status": {"id": 3, "name": "Success"}},
            {"status": {"id": 4, "name": "Failure"}},
        ):
            with self.subTest(changes=changes):
                self.engine.reconcile([record(**changes)], NOW)
                self.deliver()
                self.assertEqual(self.events(), [])
                self.assertEqual(self.sender.messages, [])

    def test_stable_launch_time_deduplicates_after_restart_and_metadata_change(self):
        self.engine.reconcile([record()], NOW)
        self.deliver()
        original_key = self.sender.messages[0][1]
        self.store.close()
        self.store = Store(self.path)
        self.engine = Engine(self.store)
        later = NOW + timedelta(minutes=1)
        self.engine.reconcile([record(name="Updated mission name")], later)
        self.engine.schedule(later)
        self.deliver(later)
        self.assertEqual(len(self.sender.messages), 1)
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.events()[0]["key"], original_key)
        self.assertIsNotNone(self.events()[0]["delivered_at"])

    def test_pending_delivery_survives_restart(self):
        self.engine.reconcile([record()], NOW)
        self.store.close()
        self.store = Store(self.path)
        self.engine = Engine(self.store)
        self.deliver(NOW + timedelta(seconds=10))
        self.assertEqual(len(self.sender.messages), 1)
        self.assertIsNotNone(self.events()[0]["delivered_at"])

    def test_delay_beyond_lead_sends_update_then_revised_time_reminder(self):
        self.engine.reconcile([record()], NOW)
        self.deliver()
        changed_at = NOW + timedelta(minutes=1)
        new_net = NOW + timedelta(hours=2)
        changed = record(new_net)
        self.engine.reconcile([changed], changed_at)
        self.deliver(changed_at)
        self.assertEqual(len(self.sender.messages), 2)
        self.assertTrue(self.sender.messages[-1][0]["title"].startswith("Launch update:"))
        self.assertIn("Launch time changed", self.sender.messages[-1][0]["message"])
        revised_due = new_net - timedelta(minutes=30)
        self.engine.reconcile([changed], revised_due)
        self.deliver(revised_due)
        self.assertEqual(len(self.sender.messages), 3)
        self.assertTrue(self.sender.messages[-1][0]["title"].startswith("Launch in 30 min:"))
        self.assertEqual([row["kind"] for row in self.events()].count("update"), 1)

    def test_changed_time_within_lead_sends_one_new_reminder(self):
        self.engine.reconcile([record()], NOW)
        self.deliver()
        changed_at = NOW + timedelta(minutes=1)
        self.engine.reconcile([record(NOW + timedelta(minutes=25))], changed_at)
        self.deliver(changed_at)
        self.assertEqual(len(self.sender.messages), 2)
        self.assertIn("Launch in 24 min", self.sender.messages[-1][0]["title"])
        self.assertEqual([row["kind"] for row in self.events()], ["reminder", "reminder"])

    def test_hold_cancels_pending_retry_and_release_can_reactivate_it(self):
        self.sender = FakeNotifier([OSError("temporary failure")])
        self.engine.reconcile([record()], NOW)
        self.deliver()
        held_at = NOW + timedelta(seconds=1)
        self.engine.reconcile([record(status={"id": 5, "name": "On Hold"})], held_at)
        self.deliver(NOW + timedelta(seconds=20))
        self.assertEqual(len(self.sender.calls), 1)
        self.assertIsNotNone(self.events()[0]["canceled_at"])
        released_at = NOW + timedelta(seconds=30)
        self.engine.reconcile([record()], released_at)
        self.deliver(released_at)
        self.assertEqual(len(self.sender.messages), 1)
        self.assertEqual(len(self.events()), 1)
        self.assertIsNone(self.events()[0]["canceled_at"])

    def test_withdrawn_stream_corrects_an_already_delivered_reminder(self):
        self.engine.reconcile([record()], NOW)
        self.deliver()
        later = NOW + timedelta(minutes=1)
        self.engine.reconcile([record(vid_urls=[])], later)
        self.deliver(later)
        self.assertEqual(len(self.sender.messages), 2)
        update = self.sender.messages[-1][0]
        self.assertIn("no longer confirmed", update["message"])
        self.assertNotIn("click", update)
        self.assertNotIn("actions", update)

    def test_hold_release_corrects_delivered_hold_update_without_duplicate_reminder(self):
        self.engine.reconcile([record()], NOW)
        self.deliver()
        held_at = NOW + timedelta(minutes=1)
        self.engine.reconcile([record(status={"id": 5, "name": "On Hold"})], held_at)
        self.deliver(held_at)
        self.assertEqual(len(self.sender.messages), 2)
        self.assertIn("no longer confirmed", self.sender.messages[-1][0]["message"])
        released_at = NOW + timedelta(minutes=2)
        self.engine.reconcile([record()], released_at)
        self.deliver(released_at)
        self.assertEqual(len(self.sender.messages), 3)
        self.assertIn("Launch is back on the schedule", self.sender.messages[-1][0]["message"])
        self.assertTrue(self.sender.messages[-1][0]["title"].startswith("Launch update:"))
        self.assertEqual(len(self.events("WHERE kind='reminder' AND delivered_at IS NOT NULL")), 1)
        later = NOW + timedelta(minutes=3)
        self.engine.reconcile([record()], later)
        self.deliver(later)
        self.assertEqual(len(self.sender.messages), 3)

    def test_stale_data_suppresses_pending_delivery_until_refreshed(self):
        self.engine.reconcile([record()], NOW)
        stale = NOW + timedelta(seconds=901)
        self.deliver(stale)
        self.engine.schedule(stale)
        self.assertEqual(self.sender.messages, [])
        self.assertIsNone(self.events()[0]["delivered_at"])
        self.engine.reconcile([record()], stale)
        self.deliver(stale)
        self.assertEqual(len(self.sender.messages), 1)

    def test_future_fetch_timestamp_is_not_fresh(self):
        self.engine.reconcile([record()], NOW)
        self.assertFalse(self.engine.fresh(NOW - timedelta(seconds=1)))
        self.deliver(NOW - timedelta(seconds=1))
        self.assertEqual(self.sender.messages, [])

    def test_catches_up_before_net_but_never_at_or_after_net(self):
        launch = record(NOW + timedelta(minutes=5))
        self.engine.reconcile([launch], NOW)
        self.deliver()
        self.assertIn("Launch in 5 min", self.sender.messages[0][0]["title"])
        for offset in (0, -1):
            with self.subTest(seconds_from_net=offset):
                self.engine.reconcile([launch, record(NOW + timedelta(seconds=offset), id=f"expired-{offset}")], NOW)
                self.deliver()
                self.assertEqual(len(self.sender.messages), 1)

    def test_expired_pending_event_cannot_be_published(self):
        launch = record(NOW + timedelta(minutes=5))
        self.engine.reconcile([launch], NOW)
        self.deliver(NOW + timedelta(minutes=5))
        self.assertEqual(self.sender.messages, [])
        self.assertIsNotNone(self.events()[0]["canceled_at"])

    def test_slow_first_delivery_cannot_send_second_event_after_net(self):
        class SlowNotifier(FakeNotifier):
            current = NOW

            def now(self):
                return self.current

            def send(self, message, key):
                super().send(message, key)
                self.current += timedelta(minutes=2)

        self.sender = SlowNotifier()
        net = NOW + timedelta(minutes=1)
        self.engine.reconcile([record(net), record(net, id="launch-2")], NOW)
        self.deliver()
        self.assertEqual(len(self.sender.messages), 1)
        self.assertEqual(len(self.events("WHERE canceled_at IS NOT NULL")), 1)

    def test_retry_backoff_preserves_pending_event_and_safe_error(self):
        self.sender = FakeNotifier([OSError("secret token and URL"), OSError("another failure")])
        self.engine.reconcile([record()], NOW)
        self.deliver()
        first = self.events()[0]
        self.assertEqual(first["attempts"], 1)
        self.assertEqual(first["error"], "OSError")
        self.assertEqual(timestamp(first["next_try"]), NOW + timedelta(seconds=10))
        self.deliver(NOW + timedelta(seconds=9))
        self.assertEqual(len(self.sender.calls), 1)
        self.deliver(NOW + timedelta(seconds=10))
        second = self.events()[0]
        self.assertEqual(second["attempts"], 2)
        self.assertEqual(timestamp(second["next_try"]), NOW + timedelta(seconds=30))
        self.deliver(NOW + timedelta(seconds=30))
        self.assertEqual(len(self.sender.messages), 1)
        self.assertIsNone(self.events()[0]["error"])

    def test_rate_limit_cools_down_all_messages_and_survives_reconciliation(self):
        self.sender = FakeNotifier([RateLimited("too many requests")])
        launches = [record(), record(id="launch-2", name="Other launch")]
        self.engine.reconcile(launches, NOW)
        self.deliver()
        self.assertEqual(len(self.sender.calls), 1)
        for row in self.events():
            self.assertGreaterEqual(timestamp(row["next_try"]), NOW + timedelta(seconds=60))
        self.engine.reconcile(launches, NOW + timedelta(seconds=30))
        self.deliver(NOW + timedelta(seconds=30))
        self.assertEqual(len(self.sender.calls), 1)
        self.deliver(NOW + timedelta(seconds=60))
        self.assertEqual(len(self.sender.messages), 2)

    def test_global_rate_limit_blocks_new_due_event_and_changed_retry(self):
        self.sender = FakeNotifier([RateLimited("too many requests")])
        later_launch = record(NOW + timedelta(minutes=30, seconds=30), id="launch-2")
        self.engine.reconcile([record(), later_launch], NOW)
        self.deliver()
        self.assertEqual(timestamp(self.store.get("delivery_retry_until")), NOW + timedelta(seconds=60))
        during_cooldown = NOW + timedelta(seconds=30)
        self.engine.reconcile([record(name="Updated mission name"), later_launch], during_cooldown)
        self.assertEqual(len(self.events()), 2)
        self.deliver(during_cooldown)
        self.assertEqual(len(self.sender.calls), 1)
        self.deliver(NOW + timedelta(seconds=59))
        self.assertEqual(len(self.sender.calls), 1)
        self.deliver(NOW + timedelta(seconds=60))
        self.assertEqual(len(self.sender.messages), 2)

    def test_global_rate_limit_survives_restart_before_new_event_is_due(self):
        self.sender = FakeNotifier([RateLimited("too many requests")])
        later_launch = record(NOW + timedelta(minutes=30, seconds=30), id="launch-2")
        self.engine.reconcile([record(), later_launch], NOW)
        self.deliver()
        self.store.close()
        self.store = Store(self.path)
        self.engine = Engine(self.store)
        during_cooldown = NOW + timedelta(seconds=30)
        self.engine.reconcile([record(), later_launch], during_cooldown)
        self.deliver(during_cooldown)
        self.assertEqual(len(self.sender.calls), 1)
        self.deliver(NOW + timedelta(seconds=60))
        self.assertEqual(len(self.sender.messages), 2)

    def test_disappearing_launch_cancels_retry_without_sending_stale_reminder(self):
        self.sender = FakeNotifier([OSError("offline")])
        self.engine.reconcile([record()], NOW)
        self.deliver()
        self.engine.reconcile([], NOW + timedelta(seconds=1))
        self.deliver(NOW + timedelta(seconds=20))
        self.assertEqual(len(self.sender.calls), 1)
        self.assertIsNotNone(self.events()[0]["canceled_at"])

    def test_disappearing_launch_sends_only_one_correction(self):
        self.engine.reconcile([record()], NOW)
        self.deliver()
        for seconds in (1, 2):
            now = NOW + timedelta(seconds=seconds)
            self.engine.reconcile([], now)
            self.deliver(now)
        self.assertEqual(len(self.sender.messages), 2)
        correction = self.sender.messages[-1][0]
        self.assertIn("no longer in the current schedule", correction["message"])
        self.assertNotIn("click", correction)
        self.assertNotIn("actions", correction)
        self.assertEqual(len(self.events("WHERE kind='withdrawn'")), 1)

    def test_reappearing_launch_cancels_pending_missing_correction(self):
        self.engine.reconcile([record()], NOW)
        self.deliver()
        self.engine.reconcile([], NOW + timedelta(seconds=1))
        self.engine.reconcile([record()], NOW + timedelta(seconds=2))
        self.deliver(NOW + timedelta(seconds=2))
        self.assertEqual(len(self.sender.messages), 1)
        missing = self.events("WHERE kind='withdrawn'")[0]
        self.assertIsNotNone(missing["canceled_at"])

    def test_reappearing_launch_corrects_delivered_withdrawal_then_can_withdraw_again(self):
        self.engine.reconcile([record()], NOW)
        self.deliver()
        missing_at = NOW + timedelta(minutes=1)
        self.engine.reconcile([], missing_at)
        self.deliver(missing_at)
        self.assertEqual(len(self.sender.messages), 2)
        restored_at = NOW + timedelta(minutes=2)
        self.engine.reconcile([record()], restored_at)
        self.deliver(restored_at)
        self.assertEqual(len(self.sender.messages), 3)
        self.assertIn("Launch is back on the schedule", self.sender.messages[-1][0]["message"])
        self.assertEqual(len(self.events("WHERE kind='reminder' AND delivered_at IS NOT NULL")), 1)
        unchanged_at = NOW + timedelta(minutes=3)
        self.engine.reconcile([record()], unchanged_at)
        self.deliver(unchanged_at)
        self.assertEqual(len(self.sender.messages), 3)
        for minutes in (4, 5):
            now = NOW + timedelta(minutes=minutes)
            self.engine.reconcile([], now)
            self.deliver(now)
        self.assertEqual(len(self.sender.messages), 4)
        self.assertIn("no longer in the current schedule", self.sender.messages[-1][0]["message"])
        self.assertEqual(len(self.events("WHERE kind='withdrawn' AND delivered_at IS NOT NULL")), 2)

    def test_invalid_or_duplicate_snapshot_does_not_partially_replace_schedule(self):
        self.engine.reconcile([record()], NOW)
        for bad in ([record(id="new"), {"id": "invalid"}], [record(), record()]):
            with self.subTest(snapshot=bad):
                with self.assertRaises(ValueError):
                    self.engine.reconcile(bad, NOW + timedelta(seconds=1))
                rows = self.store.db.execute("SELECT * FROM launches").fetchall()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["id"], "launch-1")
                self.assertEqual(rows[0]["present"], 1)
                self.assertEqual(self.store.get("last_fetch"), NOW.isoformat())


class NotificationTests(unittest.TestCase):
    def test_local_time_respects_daylight_saving_and_preserves_watch_action(self):
        for net, expected in (
            (datetime(2030, 10, 16, 12, 30, tzinfo=UTC), "07:30 AM CDT"),
            (datetime(2030, 1, 16, 12, 30, tzinfo=UTC), "06:30 AM CST"),
        ):
            with self.subTest(net=net):
                launch = Launch.from_source(record(net))
                message = notification(launch, "reminder", None, net - timedelta(minutes=30), TZ)
                self.assertIn(expected, message["message"])
                self.assertEqual(message["click"], launch.stream)
                self.assertEqual(message["actions"][0]["url"], launch.stream)
                self.assertEqual(message["priority"], 4)

    def test_rounds_up_fractional_minutes_and_bounds_title(self):
        launch = Launch.from_source(record(name="X" * 400))
        message = notification(launch, "reminder", None, NOW + timedelta(seconds=1), TZ)
        self.assertTrue(message["title"].startswith("Launch in 30 min:"))
        self.assertEqual(len(message["title"]), 240)


if __name__ == "__main__":
    unittest.main()
