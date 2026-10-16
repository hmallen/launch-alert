"""Release-critical Unraid metadata, environment, and health-check contracts."""

import contextlib
import io
import os
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from launch_alert.__main__ import main
from launch_alert.config import Config
from tools.validate_unraid import ROOT, validate


class UnraidTests(unittest.TestCase):
    def test_shipped_package_validates_and_environment_defaults_start(self):
        self.assertEqual(validate(), 1)
        app = ET.parse(ROOT / "templates/launch-alert.xml").getroot()
        env = {node.get("Target"): node.get("Default") for node in app.findall("Config") if node.get("Type") == "Variable"}
        env.update(NTFY_TOPIC="local-test-only", TZ="Europe/London")
        with patch.dict(os.environ, env, clear=True):
            config = Config.from_env()
        self.assertEqual(config.db_path, Path("/config/launch-alert.sqlite3"))
        self.assertEqual(config.timezone.key, "Europe/London")
        self.assertEqual(config.lead_minutes, 30)

    def test_time_zone_inherits_unraid_host_and_allows_explicit_override(self):
        with patch.dict(os.environ, {"NTFY_TOPIC": "local-test-only", "TZ": "Europe/London", "DISPLAY_TIMEZONE": ""}, clear=True):
            self.assertEqual(Config.from_env().timezone.key, "Europe/London")
            os.environ["DISPLAY_TIMEZONE"] = "Asia/Tokyo"
            self.assertEqual(Config.from_env().timezone.key, "Asia/Tokyo")
        with patch.dict(os.environ, {"NTFY_TOPIC": "local-test-only"}, clear=True):
            self.assertEqual(Config.from_env().timezone.key, "America/Chicago")

    def test_distribution_rejects_shared_secrets_broken_persistence_and_wrong_urls(self):
        for target, default in (("NTFY_TOPIC", "shared-topic"), ("NTFY_TOKEN", "shared-token"), ("DB_PATH", "/tmp/state.sqlite3")):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for name in ("ca_profile.xml", "LICENSE", "templates", "assets", "unraid"):
                    source = ROOT / name
                    if source.is_dir():
                        shutil.copytree(source, root / name)
                    else:
                        shutil.copy2(source, root / name)
                template = root / "templates/launch-alert.xml"
                document = ET.parse(template)
                document.find(f".//Config[@Target='{target}']").set("Default", default)
                document.write(template)
                with self.assertRaises(ValueError):
                    validate(root)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("ca_profile.xml", "LICENSE", "templates", "assets", "unraid"):
                source = ROOT / name
                if source.is_dir():
                    shutil.copytree(source, root / name)
                else:
                    shutil.copy2(source, root / name)
            template = root / "templates/launch-alert.xml"
            document = ET.parse(template)
            document.find("TemplateURL").text = "https://example.com/wrong.xml"
            document.write(template)
            with self.assertRaisesRegex(ValueError, "TemplateURL"):
                validate(root)

    def test_docker_healthcheck_reports_success_failure_and_connection_errors(self):
        for status, expected in ((200, 0), (503, 1)):
            with self.subTest(status=status):
                connection = Mock()
                connection.getresponse.return_value.status = status
                with patch("sys.argv", ["launch_alert", "healthcheck"]), patch("launch_alert.__main__.load_env"), patch("launch_alert.__main__.Config.from_env", return_value=SimpleNamespace(health_port=8080)), patch("launch_alert.__main__.HTTPConnection", return_value=connection), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(), expected)
                connection.close.assert_called_once()
        connection = Mock()
        connection.request.side_effect = ConnectionRefusedError("worker unavailable")
        with patch("sys.argv", ["launch_alert", "healthcheck"]), patch("launch_alert.__main__.load_env"), patch("launch_alert.__main__.Config.from_env", return_value=SimpleNamespace(health_port=8080)), patch("launch_alert.__main__.HTTPConnection", return_value=connection), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(), 1)
        connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
