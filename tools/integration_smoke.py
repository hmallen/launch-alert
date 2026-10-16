"""Local HTTPS integration smoke test; requires Python 3.11+, OpenSSL, and loopback sockets."""
import json
import os
import socket
import sqlite3
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

with tempfile.TemporaryDirectory(prefix="launch-alert-e2e-") as task_dir:
    root = Path(task_dir)
    cert, key = root / "cert.pem", root / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
                    "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
    launches = [{"id": "integration-launch", "name": "Fixture rocket", "net": (datetime.now(timezone.utc) + timedelta(minutes=29)).isoformat(),
                 "status": {"id": 1, "name": "Go for Launch"}, "net_precision": {"id": 0, "name": "Second"},
                 "vid_urls": [{"url": "https://example.com/livestream", "priority": 1}]}]
    publishes, fetches = [], []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path.startswith("/2.3.0/launches/?"), self.path
            assert self.headers.get("Authorization") == "Token fixture-source-token"
            fetches.append(self.path)
            self.reply({"count": len(launches), "next": None, "results": launches})

        def do_POST(self):
            assert self.headers.get("Authorization") == "Bearer fixture-ntfy-token"
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            publishes.append(body)
            self.reply({"event": "message", "id": f"message-{len(publishes)}", "topic": body["topic"]})

        def reply(self, value):
            payload = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    upstream.socket = context.wrap_socket(upstream.socket, server_side=True)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        health_port = sock.getsockname()[1]
    db = root / "state.sqlite3"
    env = {**os.environ, "PYTHONPATH": str(REPO), "ENV_FILE": "/dev/null",
           "NTFY_SERVER": f"https://localhost:{upstream.server_port}", "NTFY_TOPIC": "fixture-topic",
           "NTFY_TOKEN": "fixture-ntfy-token", "LL2_TOKEN": "fixture-source-token",
           "LL2_BASE_URL": f"https://localhost:{upstream.server_port}/2.3.0", "DB_PATH": str(db),
           "LEAD_MINUTES": "30", "POLL_SECONDS": "300", "MAX_DATA_AGE_SECONDS": "900",
           "DISPLAY_TIMEZONE": "America/Chicago", "SSL_CERT_FILE": str(cert), "NO_PROXY": "localhost,127.0.0.1",
           "HEALTH_HOST": "127.0.0.1", "HEALTH_PORT": str(health_port)}

    def command(name):
        return subprocess.run(["python", "-m", "launch_alert", name], env=env, cwd=root, capture_output=True, text=True, timeout=10)

    try:
        first = command("once")
        assert first.returncode == 0, first.stderr
        assert len(publishes) == len(fetches) == 1
        assert publishes[0]["click"] == "https://example.com/livestream"
        assert publishes[0]["actions"][0]["action"] == "view"
        assert "29 min" in publishes[0]["title"]
        second = command("once")
        assert second.returncode == 0, second.stderr
        assert len(publishes) == len(fetches) == 1

        # Force another refresh, then observe a correction for a three-hour delay.
        launches[0]["net"] = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        with sqlite3.connect(db) as connection:
            connection.execute("UPDATE metadata SET value=? WHERE key='next_fetch'", (json.dumps(datetime.now(timezone.utc).isoformat()),))
        correction = command("once")
        assert correction.returncode == 0, correction.stderr
        assert len(fetches) == len(publishes) == 2
        assert "Launch time changed" in publishes[-1]["message"]

        process = subprocess.Popen(["python", "-m", "launch_alert", "run"], cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            for attempt in range(100):
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{health_port}/healthz", timeout=0.2) as response:
                        status = json.load(response)
                        assert status["healthy"] is True
                        break
                except OSError:
                    time.sleep(0.05)
            else:
                raise AssertionError("health endpoint never became healthy")
            process.terminate()
            out, err = process.communicate(timeout=5)
            assert process.returncode == 0, err
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        print("PASS: real HTTPS source + ntfy adapters, auth, livestream action, persisted restart dedup, delay correction, health endpoint, SIGTERM shutdown; no external notifications.")
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join()
