"""Run the Unraid template's permissions and persistence contract against a local image."""

import argparse
import subprocess
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path


def docker(*args, **kwargs):
    return subprocess.run(["docker", *args], check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="launch-alert:unraid")
    args = parser.parse_args()
    app = ET.parse(Path(__file__).resolve().parents[1] / "templates/launch-alert.xml").getroot()
    volume = "launch-alert-unraid-smoke-" + uuid.uuid4().hex[:12]
    docker("volume", "create", volume, stdout=subprocess.DEVNULL)
    # Unraid uses a bind mount. Disable named-volume image copy-up so Docker
    # doesn't reset ownership of the empty test volume between containers.
    mount = f"type=volume,src={volume},dst=/config,volume-nocopy"
    try:
        # DockerMan creates fresh appdata paths as nobody:users; mimic that on a disposable volume.
        docker("run", "--rm", "--network", "none", "--user", "0:0", "--mount", mount, args.image,
               "python", "-c", "import os; os.chown('/config',99,100); os.chmod('/config',0o770)")
        command = ["run", "--rm", "--network", "none", "--user", "99:100", "--read-only", "--cap-drop", "ALL",
                   "--security-opt", "no-new-privileges", "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m", "--mount", mount]
        env = {node.get("Target"): node.get("Default") for node in app.findall("Config") if node.get("Type") == "Variable"}
        env.update(NTFY_TOPIC="local-smoke-only", TZ="Europe/London")
        for name, value in env.items():
            command.extend(["--env", f"{name}={value}"])
        command.extend([args.image, "python", "-c"])
        docker(*command, r'''
import os, subprocess, sys
from datetime import timedelta
from launch_alert.config import Config
from launch_alert.service import Worker, Health, report, utcnow
from launch_alert.storage import Store
assert (os.getuid(), os.getgid()) == (99, 100)
c = Config.from_env()
assert c.timezone.key == 'Europe/London'
assert str(c.db_path) == '/config/launch-alert.sqlite3'
class Source:
    def fetch(self, now):
        return [{'id':'fixture', 'name':'Local fixture', 'net':(now+timedelta(minutes=29)).isoformat(),
                 'status':{'id':1,'name':'Go'}, 'net_precision':{'name':'Second'},
                 'vid_urls':[{'url':'https://example.com/live'}]}]
class Sink:
    sent = 0
    def send(self, message, event_id):
        self.sent += 1
s = Store(c.db_path)
n = Sink()
Worker(c,s,Source(),n).tick()
assert n.sent == 1
health = Health('127.0.0.1', c.health_port)
health.snapshot = report(s, utcnow())
health.start()
try:
    subprocess.run([sys.executable,'-m','launch_alert','healthcheck'],check=True)
    health.snapshot = {'healthy':False}
    assert subprocess.run([sys.executable,'-m','launch_alert','healthcheck']).returncode == 1
finally:
    health.close()
    s.close()
print('PASS: Unraid UID99:GID100, read-only root, writable appdata, inherited timezone, reminder, health200/503.')
''')
        docker(*command, r'''
from launch_alert.config import Config
from launch_alert.service import Worker, report, utcnow
from launch_alert.storage import Store
class NoExtraIO:
    def fetch(self, now):
        raise AssertionError('Restart fetched before persisted refresh deadline')
    def send(self, message, event_id):
        raise AssertionError('Restart duplicated delivered reminder')
c = Config.from_env()
s = Store(c.db_path)
try:
    Worker(c,s,NoExtraIO(),NoExtraIO()).tick()
    assert report(s,utcnow())['healthy']
    assert s.db.execute('SELECT COUNT(*) FROM events WHERE delivered_at IS NOT NULL').fetchone()[0] == 1
finally:
    s.close()
print('PASS: a new container recovered persistent state without duplicate delivery or extra fetch.')
''')
    finally:
        docker("volume", "rm", volume, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
