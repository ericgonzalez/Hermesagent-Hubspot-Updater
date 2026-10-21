"""Tiny HTTP helper (urllib) with retry/backoff. Never logs auth headers."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request


class ApiError(Exception):
    def __init__(self, status, body, url):
        self.status = status
        self.body = body
        self.url = url.split("?")[0]
        super().__init__(f"HTTP {status} from {self.url}: {body[:300]}")


def _open(req, timeout, retries):
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                wait = float(e.headers.get("Retry-After", 0) or 0) or (2 ** attempt)
                time.sleep(min(wait, 30))
                continue
            raise ApiError(e.code, body, req.full_url)
        except urllib.error.URLError as e:
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise ApiError(0, str(e.reason), req.full_url)


def request_json(method, url, headers=None, params=None, body=None, timeout=30, retries=3):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
    hdrs = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    raw = _open(req, timeout, retries)
    return json.loads(raw) if raw else {}


def request_text(url, headers=None, timeout=60, retries=3):
    req = urllib.request.Request(url, headers=dict(headers or {}), method="GET")
    return _open(req, timeout, retries).decode("utf-8", "replace")
