"""Orchestration: prepare (Zoom -> match -> work files) and commit (validated extraction -> HubSpot)."""
import hashlib
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone

from . import compose, matching
from .config import rep_emails
from .hubspot import html_to_text
from .net import ApiError
from .state import TERMINAL
from .transcript import corpus_from_rendered, parse_vtt, render_numbered, word_count
from .validate import validate_extraction

REVIEW_RE_DEAL = re.compile(r"^\s*DEAL_ID:\s*([A-Za-z0-9_-]+)\s*$", re.M)
REVIEW_RE_DISMISS = re.compile(r"^\s*DISMISS\s*$", re.M | re.I)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def log(msg):
    print(msg, file=sys.stderr)


def now_utc():
    return datetime.now(timezone.utc)


def work_path(cfg, uuid):
    return os.path.join(cfg["work_dir"], hashlib.sha1(uuid.encode()).hexdigest()[:16])


def meeting_date(m):
    return (m.get("start_time") or "")[:10] or now_utc().strftime("%Y-%m-%d")


def end_iso(m):
    start = matching.parse_ts(m.get("start_time"))
    return (start + timedelta(minutes=int(m.get("duration") or 0))).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Shared write path (used by commit and by approved reviews)
# --------------------------------------------------------------------------
def write_to_deal(cfg, hs, deal, meeting, sentences, tasks, match_label, flags, owner_id):
    """Create the note/meeting (+ tasks) on the deal. Idempotent via pcix-ref marker. Returns a result dict."""
    refs = hs.deal_engagement_refs(deal["id"])
    already = [r for r in refs if meeting["uuid"] in compose.find_refs(r["body"])]
    if already:
        return {"status": "already_written", "object_id": already[0]["id"], "tasks": []}
    prior = [r for r in refs if compose.find_refs(r["body"])]
    meta = {"date": meeting_date(meeting), "call_number": len(prior) + 1, "match_label": match_label,
            "uuid": meeting["uuid"]}
    body = compose.note_html(sentences, meta, flags)
    start = meeting.get("start_time") or now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
    if cfg["write_as"] == "meeting":
        title = (meeting.get("topic") or "Zoom call")[:120]
        created = hs.create_meeting(deal["id"], title, body, start, end_iso(meeting), owner_id)
    else:
        created = hs.create_note(deal["id"], body, start, owner_id)
    task_ids, task_error = [], None
    if cfg["create_tasks"]:
        for t in tasks[: int(cfg["max_tasks_per_call"])]:
            try:
                due = t.get("due") or (now_utc() + timedelta(days=2)).strftime("%Y-%m-%d")
                r = hs.create_task(t["text"][:120], f"From Zoom call {meta['date']}.", f"{due}T17:00:00Z",
                                   owner_id, deal["id"])
                task_ids.append(r.get("id"))
            except ApiError as e:
                task_error = str(e)
                break
    return {"status": "written", "object_id": created.get("id"), "tasks": task_ids, "task_error": task_error,
            "call_number": meta["call_number"]}


# --------------------------------------------------------------------------
# prepare
# --------------------------------------------------------------------------
def collect_meetings(cfg, zoom, state, out):
    reps = set(rep_emails(cfg))
    meetings = {}
    for job in state.pending_jobs():
        if job["host_email"] not in reps:
            state.finish_job(job["uuid"], "not_allowlisted")
            continue
        try:
            m = zoom.get_meeting_recordings(job["uuid"])
            m["host_email"] = m["host_email"] or job["host_email"]
            if m["transcript_url"]:
                meetings[m["uuid"]] = m
                state.finish_job(job["uuid"], "done")
            elif time.time() - job["created"] > 24 * 3600:
                state.finish_job(job["uuid"], "no_transcript")
        except ApiError as e:
            out["errors"].append({"job": job["uuid"], "error": str(e)})
    frm = (now_utc() - timedelta(hours=int(cfg["lookback_hours"]))).strftime("%Y-%m-%d")
    to = now_utc().strftime("%Y-%m-%d")
    for rep in sorted(reps):
        try:
            for m in zoom.list_user_recordings(rep, frm, to):
                if m["transcript_url"]:
                    meetings.setdefault(m["uuid"], m)
        except ApiError as e:
            out["errors"].append({"rep": rep, "error": str(e)})
    return sorted(meetings.values(), key=lambda m: m.get("start_time") or "")


def purge(cfg, state, dry_run=False):
    """Delete finished work folders older than retention_days and old state rows."""
    root = os.path.realpath(cfg["work_dir"])
    cutoff = time.time() - int(cfg["retention_days"]) * 86400
    removed = []
    for status in ("committed", "review", "skipped", "failed"):
        for it in state.list_items(status):
            wd = it.get("work_dir")
            if not wd or it["updated"] >= cutoff or not os.path.isdir(wd):
                continue
            if not os.path.realpath(wd).startswith(root + os.sep):   # never delete outside work_dir
                continue
            removed.append(wd)
            if not dry_run:
                shutil.rmtree(wd, ignore_errors=True)
    rows = {} if dry_run else state.purge_old(int(cfg["state_retention_days"]))
    return {"work_dirs_removed": len(removed), "state_rows_removed": rows, "dry_run": dry_run}


def prepare(cfg, zoom, hs, state):
    out = {"resolved_reviews": [], "pending": [], "skipped": [], "errors": []}
    if not cfg["dry_run"]:
        out["purged"] = purge(cfg, state)
    try:
        out["resolved_reviews"] = resolve_reviews(cfg, hs, state)
    except ApiError as e:
        out["errors"].append({"step": "resolve_reviews", "error": str(e)})

    for m in collect_meetings(cfg, zoom, state, out):
        uuid = m["uuid"]
        st = state.get_item(uuid)
        if st and st["status"] in TERMINAL:
            continue
        if st and st["status"] == "prepared":
            continue  # already prepared; listed below
        try:
            text = zoom.download_transcript(m["transcript_url"])
            segs = parse_vtt(text)
            if word_count(segs) < int(cfg["min_transcript_words"]):
                _terminal(cfg, state, uuid, "skipped", "too_short")
                out["skipped"].append({"meeting": uuid, "topic": m["topic"], "reason": "too_short"})
                continue
            try:
                participants = zoom.get_participants(uuid)
            except ApiError as e:
                participants = []
                log(f"participants unavailable for {uuid}: {e}")
            match = matching.match_meeting(m, participants, hs, cfg)
            if match["internal_only"]:
                _terminal(cfg, state, uuid, "skipped", "internal_only")
                out["skipped"].append({"meeting": uuid, "topic": m["topic"], "reason": "internal_only"})
                continue
            refs = hs.deal_engagement_refs(match["deal"]["id"]) if match["deal"] else []
            if any(uuid in compose.find_refs(r["body"]) for r in refs):
                _terminal(cfg, state, uuid, "committed", "already_in_hubspot")
                out["skipped"].append({"meeting": uuid, "topic": m["topic"], "reason": "already_in_hubspot"})
                continue
            prior = sorted([d for d in (compose.footer_date(r["body"]) for r in refs) if d])
            wd = work_path(cfg, uuid)
            os.makedirs(wd, exist_ok=True)
            ctx = {"meeting": m, "match": match, "participants": participants,
                   "call_number": len(prior) + 1, "previous_call_date": prior[-1] if prior else None,
                   "max_note_sentences": int(cfg["max_note_sentences"])}
            with open(os.path.join(wd, "context.json"), "w", encoding="utf-8") as fh:
                json.dump(ctx, fh, indent=2)
            with open(os.path.join(wd, "transcript.txt"), "w", encoding="utf-8") as fh:
                fh.write(render_numbered(segs))
            state.set_item(uuid, "prepared", work_dir=wd, attempts=0)
        except ApiError as e:
            out["errors"].append({"meeting": uuid, "error": str(e)})

    for it in state.list_items("prepared"):
        wd = it["work_dir"]
        has_ext = os.path.exists(os.path.join(wd, "extraction.json"))
        if not has_ext:
            n = it["attempts"] + 1
            if n > int(cfg["max_prepare_attempts"]):
                state.set_item(it["uuid"], "failed", detail="no extraction produced")
                out["errors"].append({"meeting": it["uuid"], "error": "no extraction after retries; marked failed"})
                continue
            state.set_item(it["uuid"], "prepared", attempts=n)
        out["pending"].append({"meeting_uuid": it["uuid"], "context": os.path.join(wd, "context.json"),
                               "transcript": os.path.join(wd, "transcript.txt"),
                               "write_extraction_to": os.path.join(wd, "extraction.json"),
                               "extraction_exists": has_ext})
    return out


def _terminal(cfg, state, uuid, status, detail):
    if not cfg["dry_run"]:
        state.set_item(uuid, status, detail=detail)


# --------------------------------------------------------------------------
# commit
# --------------------------------------------------------------------------
def build_review_body(match, sentences, meeting, uuid):
    deal = match.get("deal")
    sug = f"{deal['name']} (ID {deal['id']})" if deal else "none found"
    return (
        f"Call Intel could not confidently match this Zoom call to a deal (reason: {match['reason']}).\n"
        f"Suggested deal: {sug}.\n"
        "To approve: fix the DEAL_ID line if needed, then mark this task complete.\n"
        "To dismiss: put DISMISS on its own line, then mark this task complete.\n\n"
        f"DEAL_ID: {deal['id'] if deal else ''}\n"
        f"PCIX_MEETING: {uuid}\n\n"
        f"Proposed note:\n{compose.plain_prose(sentences)}\n"
    )


def commit(cfg, hs, state, only=None):
    results = []
    for it in state.list_items("prepared"):
        uuid, wd = it["uuid"], it["work_dir"]
        if only and uuid != only:
            continue
        ext_path = os.path.join(wd, "extraction.json")
        if not os.path.exists(ext_path):
            continue
        try:
            ctx = json.loads(_read(os.path.join(wd, "context.json")))
            corpus = corpus_from_rendered(_read(os.path.join(wd, "transcript.txt")))
            try:
                raw = json.loads(_read(ext_path))
            except json.JSONDecodeError as e:
                state.set_item(uuid, "prepared", attempts=it["attempts"] + 1, detail=f"bad json: {e}")
                results.append({"meeting": uuid, "status": "error", "error": f"extraction.json invalid: {e}"})
                os.replace(ext_path, ext_path + ".bad")
                continue
            res = commit_one(cfg, hs, state, ctx, raw, corpus)
            results.append(res)
            cur = state.get_item(uuid)
            if cfg["delete_transcript_after_commit"] and not cfg["dry_run"] and cur and cur["status"] in TERMINAL:
                try:
                    os.remove(os.path.join(wd, "transcript.txt"))
                except FileNotFoundError:
                    pass
        except ApiError as e:
            results.append({"meeting": uuid, "status": "error", "error": str(e)})
    return results


def commit_one(cfg, hs, state, ctx, raw, corpus):
    m, match = ctx["meeting"], ctx["match"]
    uuid = m["uuid"]
    v = validate_extraction(raw, corpus, cfg)
    clean = v["clean"]
    res = {"meeting": uuid, "topic": m["topic"], "date": meeting_date(m), "match": match,
           "call_number": ctx["call_number"], "clean": clean, "withheld": v["withheld"], "errors": v["errors"]}

    sentences, truncated = compose.cap_sentences([s["text"] for s in clean["note_sentences"]],
                                                 int(cfg["max_note_sentences"]))
    res["sentences"], res["truncated"] = sentences, truncated
    if not sentences:
        res["status"] = "skipped_no_verified_content"
        _terminal(cfg, state, uuid, "skipped", "no_verified_content")
        return res

    tasks = [s for s in clean["next_steps"] if s["owner_side"] == "us"]
    medium = any(s["confidence"] == "medium" for s in clean["note_sentences"])
    flags = "includes medium-confidence content" if medium else None
    host_owner = hs.owner_id_by_email(m.get("host_email"))
    deal = match.get("deal")
    auto = match["confidence"] in ("high", "medium") and deal is not None

    if auto:
        owner = host_owner or deal.get("owner_id")
        w = write_to_deal(cfg, hs, deal, m, sentences, tasks, match["confidence"], flags, owner)
        res.update(w)
        res["status"] = "written" if w["status"] == "written" else "already_written"
        _terminal(cfg, state, uuid, "committed", f"deal {deal['id']} {w.get('object_id')}")
        return res

    # low confidence / no deal -> review task (no note written)
    subject = f"{cfg['review_task_prefix']} {m['topic'] or 'Zoom call'} ({meeting_date(m)})"
    body = build_review_body(match, sentences, m, uuid)
    due = (now_utc() + timedelta(days=1)).strftime("%Y-%m-%dT17:00:00Z")
    t = hs.create_task(subject, body, due, host_owner, deal["id"] if deal else None)
    proposed = {"meeting": m, "sentences": sentences, "tasks": tasks, "flags": flags,
                "call_number": ctx["call_number"], "owner_id": host_owner}
    if not cfg["dry_run"] and t.get("id"):
        state.add_review(t["id"], uuid, proposed)
    res.update({"status": "review_task_created", "review_task_id": t.get("id")})
    _terminal(cfg, state, uuid, "review", f"task {t.get('id')}")
    return res


# --------------------------------------------------------------------------
# reviews: rep completes the HubSpot task -> we write the proposed note
# --------------------------------------------------------------------------
def resolve_reviews(cfg, hs, state):
    opens = state.open_reviews()
    if not opens:
        return []
    found = {str(t["id"]): t for t in hs.read_tasks([r["task_id"] for r in opens])}
    results, expiry = [], int(cfg["review_expiry_days"]) * 86400
    for r in opens:
        tid, p = r["task_id"], r["proposed"]
        t = found.get(tid)
        if t is None:
            state.close_review(tid, "dismissed")
            results.append({"task": tid, "result": "dismissed (task deleted)"})
            continue
        props = t.get("properties", {})
        if props.get("hs_task_status") != "COMPLETED":
            if time.time() - r["created"] > expiry:
                state.close_review(tid, "expired")
                results.append({"task": tid, "result": "expired"})
            continue
        body = html_to_text(props.get("hs_task_body"))
        if REVIEW_RE_DISMISS.search(body):
            state.close_review(tid, "dismissed")
            results.append({"task": tid, "result": "dismissed"})
            continue
        mm = REVIEW_RE_DEAL.search(body)
        if not mm:
            state.close_review(tid, "no_deal_id")
            results.append({"task": tid, "result": "completed without a DEAL_ID; nothing written"})
            continue
        deals = hs.read_deals([mm.group(1)])
        if not deals:
            state.close_review(tid, "invalid_deal")
            results.append({"task": tid, "result": f"deal {mm.group(1)} not found; nothing written"})
            continue
        deal = matching.deal_view(deals[0])
        w = write_to_deal(cfg, hs, deal, p["meeting"], p["sentences"], p["tasks"], "confirmed by rep",
                          p.get("flags"), p.get("owner_id") or deal.get("owner_id"))
        state.close_review(tid, "approved")
        results.append({"task": tid, "result": f"approved -> deal {deal['id']} ({w['status']})"})
    return results


# --------------------------------------------------------------------------
# rep-ready output
# --------------------------------------------------------------------------
def render_summary(res):
    mt = res.get("match", {})
    deal = mt.get("deal")
    lines = [f"TRANSCRIPT: {res.get('topic') or 'Zoom call'} - {res.get('date')}",
             f"DEAL: {deal['name'] + ' (ID ' + deal['id'] + ')' if deal else 'no deal matched'}"
             + (f" - picked by latest-updated rule among {mt['candidates']}" if mt.get("selected_by") == "latest_updated" else ""),
             f"CALL SEQUENCE: #{res.get('call_number')}",
             f"MATCH CONFIDENCE: {mt.get('confidence')} ({mt.get('reason')})",
             f"RESULT: {res.get('status')}"
             + (f" (review task {res['review_task_id']})" if res.get("review_task_id") else "")]
    if res.get("sentences"):
        lines += ["", "HUBSPOT NOTE:", "  " + " ".join(res["sentences"])]
        if res.get("truncated"):
            lines.append("  (trimmed to the sentence cap)")
    c = res.get("clean", {})
    if c.get("objections"):
        lines += ["", "OBJECTIONS:"] + [f"- {o['text']} ({o.get('speaker', 'unknown')})" for o in c["objections"]]
    if c.get("timeline_signals"):
        lines += ["", "TIMELINE SIGNALS:"] + [f"- {t['text']}" for t in c["timeline_signals"]]
    if c.get("next_steps"):
        lines += ["", "NEXT STEPS:"] + [f"- [{n['owner_side']}] {n['text']}" + (f" by {n['due']}" if n.get("due") else "")
                                        for n in c["next_steps"]]
    if c.get("contact_roles"):
        lines += ["", "CONTACT ROLES:"] + [f"- {r.get('name', '?')}: {r.get('role')}" for r in c["contact_roles"]]
    if c.get("sentiment"):
        lines += ["", f"SENTIMENT: {c['sentiment']['value']}"]
    if res.get("withheld"):
        lines += ["", "WITHHELD (not written):"] + [f"- {w['field']}: {w['text']} [{w['reason']}]" for w in res["withheld"]]
    if res.get("task_error"):
        lines += ["", f"TASK ERROR: {res['task_error']}"]
    if res.get("error"):
        lines += ["", f"ERROR: {res['error']}"]
    return "\n".join(lines)
