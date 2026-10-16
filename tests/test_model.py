from datetime import datetime, timezone
import unittest

from launch_alert.model import Launch, timestamp


def record(**changes):
    value = {
        "id": "launch-1",
        "name": "  Falcon 9 | Test mission  ",
        "net": "2030-10-16T12:30:00Z",
        "status": {"id": 1, "name": "Go"},
        "net_precision": {"name": "Second"},
        "vid_urls": [{"url": "https://example.com/watch", "priority": 1}],
    }
    value.update(changes)
    return value


class LaunchTests(unittest.TestCase):
    def test_normalizes_timestamp_name_and_livestream(self):
        launch = Launch.from_source(record(net="2030-10-16T07:30:00-05:00"))
        self.assertEqual(launch.name, "Falcon 9 | Test mission")
        self.assertEqual(launch.net, "2030-10-16T12:30:00+00:00")
        self.assertEqual(launch.time, datetime(2030, 10, 16, 12, 30, tzinfo=timezone.utc))
        self.assertTrue(launch.eligible)
        self.assertEqual(Launch(**launch.to_dict()), launch)

    def test_selects_lowest_priority_usable_https_stream(self):
        launch = Launch.from_source(record(vid_urls=[
            {"url": "http://example.com/insecure", "priority": -1},
            {"url": "https://user:password@example.com/private", "priority": 0},
            {"url": "https://example.com/backup", "priority": 2},
            {"url": "https://example.com/preferred", "priority": 1},
            {"url": "javascript:alert(1)", "priority": -10},
            {"url": "https://[invalid", "priority": -10},
            {"url": None},
            None,
        ]))
        self.assertEqual(launch.stream, "https://example.com/preferred")

    def test_supports_legacy_video_field(self):
        raw = record()
        del raw["vid_urls"]
        raw["vidURLs"] = [{"url": "https://example.com/legacy"}]
        self.assertEqual(Launch.from_source(raw).stream, "https://example.com/legacy")

    def test_only_precise_go_or_tbc_launches_with_stream_are_eligible(self):
        for status_id in (1, 8):
            for precision in ("second", "Minute", "SECOND"):
                with self.subTest(status_id=status_id, precision=precision):
                    self.assertTrue(Launch.from_source(record(
                        status={"id": status_id}, net_precision={"name": precision},
                    )).eligible)
        for changes in (
            {"status": {"id": 2, "name": "TBD"}},
            {"status": {"id": 3, "name": "Success"}},
            {"status": {"id": 4, "name": "Failure"}},
            {"status": {"id": 5, "name": "On Hold"}},
            {"status": {"id": 6, "name": "In Flight"}},
            {"status": {"id": 7, "name": "Partial Failure"}},
            {"status": {"id": 9, "name": "Deployed"}},
            {"status": {"id": True}},
            {"status": "Go"},
            {"net_precision": {"name": "Hour"}},
            {"net_precision": None},
            {"net": "2030-10-16T12:30:00"},
            {"net": "not a timestamp"},
            {"vid_urls": []},
            {"vid_urls": [{"url": "http://example.com/watch"}]},
        ):
            with self.subTest(changes=changes):
                self.assertFalse(Launch.from_source(record(**changes)).eligible)

    def test_malformed_precision_names_are_ineligible(self):
        for name in (None, 42, [], {}):
            with self.subTest(name=name):
                self.assertFalse(Launch.from_source(record(net_precision={"name": name})).eligible)

    def test_missing_stable_identity_is_rejected(self):
        for changes in ({"id": None}, {"id": ""}, {"id": 4}, {"name": None}, {"name": "  "}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    Launch.from_source(record(**changes))


class TimestampTests(unittest.TestCase):
    def test_rejects_invalid_and_naive_timestamps(self):
        for value in (None, "", 123, "bad", "2030-10-16T12:30:00"):
            with self.subTest(value=value):
                self.assertIsNone(timestamp(value))

    def test_offsets_refer_to_the_same_utc_instant(self):
        self.assertEqual(timestamp("2030-10-16T07:30:00-05:00"), timestamp("2030-10-16T12:30:00Z"))


if __name__ == "__main__":
    unittest.main()
