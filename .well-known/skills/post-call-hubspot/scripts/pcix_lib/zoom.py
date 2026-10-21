"""Zoom client: Server-to-Server OAuth (one account-level app), read-only."""
import base64
import os
import time
import urllib.parse

from .net import ApiError, request_json, request_text


def enc_uuid(uuid):
    """Zoom requires double URL-encoding for UUIDs that start with '/' or contain '//'."""
    if uuid.startswith("/") or "//" in uuid:
        return urllib.parse.quote(urllib.parse.quote(uuid, safe=""), safe="")
    return urllib.parse.quote(uuid, safe="")


def normalize_meeting(m, host_email=None):
    """Turn a Zoom recording-list/webhook meeting object into our flat meeting dict."""
    files = m.get("recording_files") or []
    transcript = None
    for f in files:
        ftype = (f.get("file_type") or "").upper()
        rtype = (f.get("recording_type") or "").lower()
        status = (f.get("status") or "completed").lower()
        if (ftype == "TRANSCRIPT" or rtype == "audio_transcript") and status == "completed":
            transcript = f.get("download_url")
            break
    return {
        "uuid": m.get("uuid", ""),
        "id": str(m.get("id", "")),
        "topic": m.get("topic", "") or "",
        "start_time": m.get("start_time", ""),
        "duration": int(m.get("duration") or 0),
        "host_email": (host_email or m.get("host_email") or "").lower(),
        "transcript_url": transcript,
    }


class ZoomClient:
    def __init__(self, cfg, env=None):
        env = env if env is not None else os.environ
        self.api = cfg["zoom"]["api_base"].rstrip("/")
        self.oauth_url = cfg["zoom"]["oauth_url"]
        self.account_id = env.get("ZOOM_ACCOUNT_ID", "")
        self.client_id = env.get("ZOOM_CLIENT_ID", "")
        self.client_secret = env.get("ZOOM_CLIENT_SECRET", "")
        self._token = None
        self._exp = 0

    def token(self):
        if self._token and time.time() < self._exp - 60:
            return self._token
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        r = request_json(
            "POST", self.oauth_url,
            headers={"Authorization": f"Basic {basic}"},
            params={"grant_type": "account_credentials", "account_id": self.account_id},
        )
        self._token = r["access_token"]
        self._exp = time.time() + int(r.get("expires_in", 3600))
        return self._token

    def _get(self, path, params=None):
        return request_json("GET", self.api + path, headers={"Authorization": f"Bearer {self.token()}"}, params=params)

    def list_user_recordings(self, user_email, date_from, date_to):
        """All cloud recordings for one rep in [date_from, date_to] (YYYY-MM-DD)."""
        out, page = [], None
        while True:
            params = {"from": date_from, "to": date_to, "page_size": 300}
            if page:
                params["next_page_token"] = page
            r = self._get(f"/users/{urllib.parse.quote(user_email, safe='')}/recordings", params)
            out += r.get("meetings", [])
            page = r.get("next_page_token")
            if not page:
                break
        return [normalize_meeting(m, host_email=user_email) for m in out]

    def get_meeting_recordings(self, meeting_uuid):
        r = self._get(f"/meetings/{enc_uuid(meeting_uuid)}/recordings")
        return normalize_meeting(r)

    def get_participants(self, meeting_uuid):
        """Past-meeting participants as [{'name','email'}], de-duplicated."""
        seen, out, page = set(), [], None
        while True:
            params = {"page_size": 300}
            if page:
                params["next_page_token"] = page
            r = self._get(f"/past_meetings/{enc_uuid(meeting_uuid)}/participants", params)
            for p in r.get("participants", []):
                email = (p.get("user_email") or "").strip().lower()
                name = (p.get("name") or "").strip()
                key = email or name.lower()
                if key and key not in seen:
                    seen.add(key)
                    out.append({"name": name, "email": email})
            page = r.get("next_page_token")
            if not page:
                break
        return out

    def download_transcript(self, url):
        return request_text(url, headers={"Authorization": f"Bearer {self.token()}"})
