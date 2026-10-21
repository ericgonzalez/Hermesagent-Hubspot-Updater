"""Optional Zoom webhook receiver. It only validates + queues a job; processing happens in `prepare`.
Put it behind HTTPS (Zoom requires a public https endpoint)."""
import hashlib
import hmac
import json
import os
import subprocess
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from .config import rep_emails
from .state import State

EVENTS = {"recording.transcript_completed", "recording.completed"}


def sign(secret, message):
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def verify(secret, timestamp, body, signature, now=None, tolerance=300):
    try:
        if abs((now or time.time()) - int(timestamp)) > tolerance:
            return False
    except (TypeError, ValueError):
        return False
    expected = "v0=" + sign(secret, f"v0:{timestamp}:{body}")
    return hmac.compare_digest(expected, signature or "")


def handle_event(cfg, state, event):
    """Returns (http_status, response_dict)."""
    if event.get("event") == "endpoint.url_validation":
        token = (event.get("payload") or {}).get("plainToken", "")
        return 200, {"plainToken": token, "encryptedToken": sign(os.environ.get("ZOOM_WEBHOOK_SECRET_TOKEN", ""), token)}
    if event.get("event") not in EVENTS:
        return 200, {"ignored": True}
    obj = (event.get("payload") or {}).get("object") or {}
    host = (obj.get("host_email") or "").lower()
    if host not in rep_emails(cfg):
        return 200, {"ignored": "host not in reps allowlist"}
    state.enqueue_job(obj.get("uuid", ""), obj.get("id", ""), host, obj.get("topic", ""))
    return 200, {"queued": True}


def serve(cfg):
    secret = os.environ.get("ZOOM_WEBHOOK_SECRET_TOKEN", "")
    if not secret:
        raise SystemExit("ZOOM_WEBHOOK_SECRET_TOKEN is required to run the webhook receiver.")
    state = State(cfg["state_db"])
    wh = cfg["webhook"]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, code, obj):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if self.path != wh["path"]:
                return self._reply(404, {"error": "not found"})
            n = int(self.headers.get("Content-Length", 0))
            if n > 1_000_000:
                return self._reply(413, {"error": "too large"})
            body = self.rfile.read(n).decode("utf-8", "replace")
            try:
                event = json.loads(body)
            except json.JSONDecodeError:
                return self._reply(400, {"error": "bad json"})
            # Zoom's URL-validation handshake is not signed; everything else must be.
            if event.get("event") != "endpoint.url_validation":
                if not verify(secret, self.headers.get("x-zm-request-timestamp"), body,
                              self.headers.get("x-zm-signature")):
                    return self._reply(401, {"error": "bad signature"})
            code, resp = handle_event(cfg, state, event)
            self._reply(code, resp)
            if resp.get("queued") and wh.get("trigger_command"):
                subprocess.Popen(wh["trigger_command"], shell=True)  # operator-configured command

    srv = HTTPServer((wh["host"], int(wh["port"])), Handler)
    print(f"webhook listening on {wh['host']}:{wh['port']}{wh['path']}", flush=True)
    srv.serve_forever()
