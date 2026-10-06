"""HubSpot client. Reads are generic; WRITES are an explicit allowlist:
create notes, meetings and tasks only. There is no method that updates deal/contact/company
properties or deal stage, by design."""
import os
import re

from .net import ApiError, request_json

DEAL_PROPS = ["dealname", "dealstage", "pipeline", "hs_lastmodifieddate", "closedate",
              "hs_is_closed", "hubspot_owner_id", "amount"]
_WRITE_PATHS = {"/crm/v3/objects/notes", "/crm/v3/objects/meetings", "/crm/v3/objects/tasks"}


class HubSpotClient:
    def __init__(self, cfg, env=None, dry_run=False):
        env = env if env is not None else os.environ
        self.base = cfg["hubspot"]["base_url"].rstrip("/")
        self.assoc = cfg["hubspot"]["assoc_ids"]
        self.token = env.get("HUBSPOT_TOKEN", "")
        self.dry_run = dry_run
        self._owner_cache = {}

    # ---- plumbing -------------------------------------------------------
    def _headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    def _read(self, method, path, params=None, body=None):
        return request_json(method, self.base + path, headers=self._headers(), params=params, body=body)

    def _write(self, path, body):
        if path not in _WRITE_PATHS:
            raise PermissionError(f"write to {path} is not allowed")
        if self.dry_run:
            return {"id": "dry-run", "dry_run": True, "path": path, "body": body}
        return request_json("POST", self.base + path, headers=self._headers(), body=body)

    # ---- reads ----------------------------------------------------------
    def search(self, obj, filters, props, limit=10):
        body = {"filterGroups": [{"filters": filters}], "properties": props, "limit": limit}
        return self._read("POST", f"/crm/v3/objects/{obj}/search", body=body).get("results", [])

    def batch_read(self, obj, ids, props):
        out, ids = [], [str(i) for i in ids]
        for i in range(0, len(ids), 100):
            body = {"properties": props, "inputs": [{"id": x} for x in ids[i:i + 100]]}
            out += self._read("POST", f"/crm/v3/objects/{obj}/batch/read", body=body).get("results", [])
        return out

    def _assoc_ids(self, from_obj, from_id, to_obj):
        ids, after = [], None
        while True:
            params = {"limit": 500}
            if after:
                params["after"] = after
            r = self._read("GET", f"/crm/v4/objects/{from_obj}/{from_id}/associations/{to_obj}", params=params)
            ids += [str(x["toObjectId"]) for x in r.get("results", [])]
            after = ((r.get("paging") or {}).get("next") or {}).get("after")
            if not after:
                break
        return ids

    def find_contact_by_email(self, email):
        r = self.search("contacts", [{"propertyName": "email", "operator": "EQ", "value": email.lower()}],
                        ["email", "firstname", "lastname", "associatedcompanyid"], limit=1)
        return r[0] if r else None

    def contact_company_ids(self, contact_id):
        return self._assoc_ids("contacts", contact_id, "companies")

    def contact_deal_ids(self, contact_id):
        return self._assoc_ids("contacts", contact_id, "deals")

    def company_deal_ids(self, company_id):
        return self._assoc_ids("companies", company_id, "deals")

    def find_company_by_domain(self, domain):
        r = self.search("companies", [{"propertyName": "domain", "operator": "EQ", "value": domain.lower()}],
                        ["name", "domain"], limit=1)
        return r[0] if r else None

    def read_deals(self, ids):
        return self.batch_read("deals", ids, DEAL_PROPS) if ids else []

    def search_deals_by_name(self, token):
        return self.search("deals", [{"propertyName": "dealname", "operator": "CONTAINS_TOKEN", "value": token}],
                           DEAL_PROPS, limit=10)

    def owner_id_by_email(self, email):
        email = (email or "").lower()
        if not email:
            return None
        if email not in self._owner_cache:
            r = self._read("GET", "/crm/v3/owners", params={"email": email, "limit": 1}).get("results", [])
            self._owner_cache[email] = str(r[0]["id"]) if r else None
        return self._owner_cache[email]

    def deal_engagement_refs(self, deal_id):
        """Notes and meetings on a deal: [{'kind','id','body'}] (used for idempotency + call numbering)."""
        refs = []
        for kind, obj, prop in (("note", "notes", "hs_note_body"), ("meeting", "meetings", "hs_meeting_body")):
            ids = self._assoc_ids("deals", deal_id, obj)
            for r in self.batch_read(obj, ids, [prop]):
                refs.append({"kind": kind, "id": r["id"], "body": (r.get("properties") or {}).get(prop) or ""})
        return refs

    def token_scopes(self):
        """Scopes granted to this private app token (HubSpot access-token-info endpoint). Read-only."""
        r = request_json("POST", self.base + "/oauth/v2/private-apps/get/access-token-info",
                         headers=self._headers(), body={"tokenKey": self.token})
        return sorted(r.get("scopes", []))

    def read_tasks(self, ids):
        return self.batch_read("tasks", ids, ["hs_task_status", "hs_task_body", "hs_task_subject"])

    # ---- allowlisted writes ---------------------------------------------
    def _assoc_to_deal(self, deal_id, type_id):
        return [{"to": {"id": str(deal_id)},
                 "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": int(type_id)}]}]

    def create_note(self, deal_id, body_html, timestamp_iso, owner_id=None):
        props = {"hs_note_body": body_html, "hs_timestamp": timestamp_iso}
        if owner_id:
            props["hubspot_owner_id"] = owner_id
        return self._write("/crm/v3/objects/notes",
                           {"properties": props, "associations": self._assoc_to_deal(deal_id, self.assoc["note_to_deal"])})

    def create_meeting(self, deal_id, title, body_html, start_iso, end_iso, owner_id=None):
        props = {"hs_timestamp": start_iso, "hs_meeting_title": title, "hs_meeting_body": body_html,
                 "hs_meeting_start_time": start_iso, "hs_meeting_end_time": end_iso,
                 "hs_meeting_outcome": "COMPLETED"}
        if owner_id:
            props["hubspot_owner_id"] = owner_id
        return self._write("/crm/v3/objects/meetings",
                           {"properties": props, "associations": self._assoc_to_deal(deal_id, self.assoc["meeting_to_deal"])})

    def create_task(self, subject, body, due_iso, owner_id=None, deal_id=None):
        props = {"hs_task_subject": subject[:200], "hs_task_body": body, "hs_task_status": "NOT_STARTED",
                 "hs_task_priority": "MEDIUM", "hs_task_type": "TODO", "hs_timestamp": due_iso}
        if owner_id:
            props["hubspot_owner_id"] = owner_id
        payload = {"properties": props}
        if deal_id:
            payload["associations"] = self._assoc_to_deal(deal_id, self.assoc["task_to_deal"])
        return self._write("/crm/v3/objects/tasks", payload)


def html_to_text(s):
    """HubSpot returns UI-edited task bodies as HTML; flatten to lines for parsing."""
    import html as _html
    s = re.sub(r"(?i)<br\s*/?>|</(p|div|li)>", "\n", s or "")
    s = re.sub(r"<[^>]+>", "", s)
    return _html.unescape(s)
