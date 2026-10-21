"""Deterministic meeting -> HubSpot deal matching. No LLM involved.

Rule of record: attendees -> client (company) -> that company's deals.
If the company has more than one deal, ALWAYS pick the most recently updated deal
(hs_lastmodifieddate). Topic-only matches never auto-write (low confidence -> review)."""
import difflib
import re
from datetime import datetime, timezone

FREE_MAIL = {"gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com", "live.com",
             "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com", "msn.com", "gmx.com"}
TOPIC_STOP = {"call", "meeting", "demo", "sync", "intro", "introduction", "follow", "followup", "discovery",
              "check", "catch", "weekly", "review", "zoom", "personal", "with", "and", "the", "for", "kickoff"}


def parse_ts(s):
    if not s:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def deal_view(d):
    p = d.get("properties", {}) if "properties" in d else d
    return {"id": str(d.get("id") or p.get("id")), "name": p.get("dealname") or "(unnamed deal)",
            "stage": p.get("dealstage"), "last_modified": p.get("hs_lastmodifieddate"),
            "is_closed": str(p.get("hs_is_closed", "")).lower() == "true",
            "owner_id": p.get("hubspot_owner_id")}


def select_deal(deals, include_closed=True):
    """Latest-updated rule. Returns (deal_view or None, candidate_count)."""
    views = [deal_view(d) for d in deals]
    if not include_closed:
        views = [v for v in views if not v["is_closed"]]
    if not views:
        return None, 0
    views.sort(key=lambda v: (parse_ts(v["last_modified"]), v["id"]), reverse=True)
    return views[0], len(views)


def classify_participants(participants, host_email, internal_domains):
    host = (host_email or "").lower()
    internal, external, unknown, seen = [], [], [], set()
    for p in participants:
        email = (p.get("email") or "").strip().lower()
        if not email:
            unknown.append(p)
            continue
        if email in seen:
            continue
        seen.add(email)
        dom = email.rsplit("@", 1)[-1]
        if email == host or dom in internal_domains:
            internal.append(p)
        else:
            external.append({**p, "email": email})
    return internal, external, unknown


def _empty(**kw):
    base = {"confidence": "low", "reason": "", "company_id": None, "company_name": None, "deal": None,
            "candidates": 0, "selected_by": None, "contact_ids": [], "external": [], "internal_only": False}
    base.update(kw)
    return base


def topic_suggestion(topic, hs):
    tokens = [t for t in re.findall(r"[A-Za-z0-9&]+", topic or "")
              if len(t) >= 4 and t.lower() not in TOPIC_STOP]
    tokens = sorted(set(tokens), key=len, reverse=True)[:2]
    best, best_score = None, 0.0
    norm_topic = " ".join(re.findall(r"[a-z0-9]+", (topic or "").lower()))
    sig = {t.lower() for t in tokens}
    for t in tokens:
        for d in hs.search_deals_by_name(t):
            v = deal_view(d)
            name = " ".join(re.findall(r"[a-z0-9]+", v["name"].lower()))
            overlap = len(sig & set(name.split())) / len(sig) if sig else 0.0
            score = max(difflib.SequenceMatcher(None, norm_topic, name).ratio(), overlap)
            if score > best_score:
                best, best_score = v, score
    return best if best_score >= 0.5 else None


def match_meeting(meeting, participants, hs, cfg):
    internal, external, unknown = classify_participants(participants, meeting.get("host_email"), cfg["internal_domains"])
    include_closed = cfg["deal_selection"].get("include_closed", True)

    if participants and not external and not unknown:
        return _empty(reason="internal_only", internal_only=True)

    votes = {}  # company_id -> {"name","contact","domain"}
    contact_ids, orphan_contacts = [], []
    domain_cache = {}
    for p in external:
        c = hs.find_contact_by_email(p["email"])
        if c:
            contact_ids.append(c["id"])
            props = c.get("properties", {})
            cid = props.get("associatedcompanyid")
            if not cid:
                cos = hs.contact_company_ids(c["id"])
                cid = cos[0] if cos else None
            if cid:
                votes.setdefault(cid, {"name": None, "contact": 0, "domain": 0})["contact"] += 1
            else:
                orphan_contacts.append(c["id"])
            continue
        dom = p["email"].rsplit("@", 1)[-1]
        if dom in FREE_MAIL:
            continue
        if dom not in domain_cache:
            domain_cache[dom] = hs.find_company_by_domain(dom)
        comp = domain_cache[dom]
        if comp:
            v = votes.setdefault(comp["id"], {"name": None, "contact": 0, "domain": 0})
            v["domain"] += 1
            v["name"] = (comp.get("properties") or {}).get("name")

    ranked = sorted(votes.items(), key=lambda kv: kv[1]["contact"] * 2 + kv[1]["domain"], reverse=True)
    score = lambda v: v["contact"] * 2 + v["domain"]
    tie = len(ranked) > 1 and score(ranked[0][1]) == score(ranked[1][1])
    common = {"contact_ids": contact_ids, "external": [e["email"] for e in external]}

    # 1) Clear winning company -> its deals -> latest updated
    if ranked and not tie:
        cid, v = ranked[0]
        deal, n = select_deal(hs.read_deals(hs.company_deal_ids(cid)), include_closed)
        if not deal:
            return _empty(reason="company_has_no_eligible_deals", company_id=cid, company_name=v["name"], **common)
        conflict = len(ranked) > 1
        if v["contact"] >= 1 and not conflict:
            conf, reason = "high", "contact_email_match"
        elif v["contact"] >= 1:
            conf, reason = "medium", "multiple_companies_majority"
        else:
            conf, reason = "medium", "domain_match"
        return _empty(confidence=conf, reason=reason, company_id=cid, company_name=v["name"], deal=deal,
                      candidates=n, selected_by="latest_updated" if n > 1 else "only_deal", **common)

    # 2) Tied companies -> suggest latest deal across them, never auto-write
    if tie:
        tied = [cid for cid, v in ranked if score(v) == score(ranked[0][1])]
        deals = [d for cid in tied for d in hs.read_deals(hs.company_deal_ids(cid))]
        deal, n = select_deal(deals, include_closed)
        return _empty(reason="ambiguous_company", deal=deal, candidates=n, **common)

    # 3) Contacts exist but have no company -> their deals, latest updated
    if orphan_contacts:
        ids = sorted({d for c in orphan_contacts for d in hs.contact_deal_ids(c)})
        deal, n = select_deal(hs.read_deals(ids), include_closed)
        if deal:
            return _empty(confidence="medium", reason="contact_without_company", deal=deal, candidates=n,
                          selected_by="latest_updated" if n > 1 else "only_deal", **common)

    # 4) Topic-only fallback: suggestion for human review, always low
    sug = topic_suggestion(meeting.get("topic", ""), hs)
    return _empty(reason="topic_only_suggestion" if sug else "no_match", deal=sug, candidates=1 if sug else 0, **common)
