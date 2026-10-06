"""In-memory stand-ins for the Zoom and HubSpot clients (same method names as the real ones)."""
from pcix_lib import compose


class FakeHubSpot:
    def __init__(self):
        self.contacts = {}        # email -> {"id", "company"}
        self.companies = {}       # id -> {"name","domain","deals":[ids]}
        self.deals = {}           # id -> properties dict
        self.contact_deals = {}
        self.owners = {}
        self.created = []         # {"kind","id","deal","body"/..}
        self.tasks = {}           # id -> {"status","body"}
        self._n = 1000
        self.dry_run = False

    def _id(self):
        self._n += 1
        return str(self._n)

    # reads
    def find_contact_by_email(self, email):
        c = self.contacts.get(email.lower())
        return {"id": c["id"], "properties": {"associatedcompanyid": c.get("company")}} if c else None

    def contact_company_ids(self, cid):
        return []

    def contact_deal_ids(self, cid):
        return self.contact_deals.get(cid, [])

    def company_deal_ids(self, cid):
        return self.companies[cid]["deals"]

    def find_company_by_domain(self, domain):
        for cid, c in self.companies.items():
            if c["domain"] == domain:
                return {"id": cid, "properties": {"name": c["name"], "domain": domain}}
        return None

    def read_deals(self, ids):
        return [{"id": i, "properties": self.deals[i]} for i in ids if i in self.deals]

    def search_deals_by_name(self, token):
        return [{"id": i, "properties": p} for i, p in self.deals.items() if token.lower() in p["dealname"].lower()]

    def owner_id_by_email(self, email):
        return self.owners.get((email or "").lower())

    def deal_engagement_refs(self, deal_id):
        return [{"kind": c["kind"], "id": c["id"], "body": c["body"]} for c in self.created
                if c.get("deal") == deal_id and c["kind"] in ("note", "meeting")]

    def read_tasks(self, ids):
        return [{"id": i, "properties": {"hs_task_status": self.tasks[i]["status"], "hs_task_body": self.tasks[i]["body"]}}
                for i in ids if i in self.tasks]

    # writes
    def create_note(self, deal_id, body, ts, owner_id=None):
        i = self._id()
        self.created.append({"kind": "note", "id": i, "deal": deal_id, "body": body, "owner": owner_id, "ts": ts})
        return {"id": i}

    def create_meeting(self, deal_id, title, body, s, e, owner_id=None):
        i = self._id()
        self.created.append({"kind": "meeting", "id": i, "deal": deal_id, "body": body, "title": title, "owner": owner_id})
        return {"id": i}

    def create_task(self, subject, body, due, owner_id=None, deal_id=None):
        i = self._id()
        self.created.append({"kind": "task", "id": i, "deal": deal_id, "subject": subject, "body": body, "due": due})
        self.tasks[i] = {"status": "NOT_STARTED", "body": body}
        return {"id": i}


class FakeZoom:
    def __init__(self, meetings, transcripts, participants):
        self.meetings, self.transcripts, self.participants = meetings, transcripts, participants

    def list_user_recordings(self, rep, f, t):
        return [m for m in self.meetings if m["host_email"] == rep]

    def get_meeting_recordings(self, uuid):
        return next(m for m in self.meetings if m["uuid"] == uuid)

    def get_participants(self, uuid):
        return self.participants[uuid]

    def download_transcript(self, url):
        return self.transcripts[url]
