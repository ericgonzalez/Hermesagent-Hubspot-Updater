#!/usr/bin/env python3
"""Post-Call Intel Extractor CLI.

  pcix.py doctor            check config, credentials and API reachability
  pcix.py prepare           resolve approved reviews, pull new Zoom transcripts, match deals, write work files
  pcix.py commit            validate the agent's extraction.json files and write to HubSpot
  pcix.py reviews           only resolve completed review tasks
  pcix.py webhook           run the optional Zoom webhook receiver (queues jobs)
  pcix.py purge             delete finished work folders / old state rows per retention settings
  pcix.py status            counts of items by state

Global flags: --config PATH   --dry-run (reads only; no HubSpot writes, no state changes)
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pcix_lib import pipeline  # noqa: E402
from pcix_lib.config import load_config, rep_emails, validate_config  # noqa: E402
from pcix_lib.hubspot import HubSpotClient  # noqa: E402
from pcix_lib.net import ApiError  # noqa: E402
from pcix_lib.state import State  # noqa: E402
from pcix_lib.zoom import ZoomClient  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["doctor", "prepare", "commit", "reviews", "webhook", "status", "purge"])
    ap.add_argument("--config")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", help="commit: only this meeting uuid")
    args = ap.parse_args(argv)
    os.umask(0o077)   # everything we create (state.db, work files) is owner-only

    cfg = load_config(args.config, overrides={"dry_run": True} if args.dry_run else None)
    need_zoom = args.command in ("doctor", "prepare")
    need_hs = args.command in ("doctor", "prepare", "commit", "reviews")
    problems = validate_config(cfg, need_zoom=need_zoom, need_hubspot=need_hs)
    if args.command != "doctor" and args.command != "status" and problems:
        print(json.dumps({"error": "config", "problems": problems}, indent=2))
        return 2

    if args.command == "doctor":
        return doctor(cfg, problems)
    if args.command == "webhook":
        from pcix_lib.webhook import serve
        serve(cfg)
        return 0

    state = State(cfg["state_db"])
    if args.command == "status":
        print(json.dumps(state.counts(), indent=2))
        return 0
    hs = HubSpotClient(cfg, dry_run=cfg["dry_run"])

    if args.command == "purge":
        print(json.dumps(pipeline.purge(cfg, state, dry_run=cfg["dry_run"]), indent=2))
        return 0
    if args.command == "reviews":
        print(json.dumps(pipeline.resolve_reviews(cfg, hs, state), indent=2))
        return 0
    if args.command == "prepare":
        out = pipeline.prepare(cfg, ZoomClient(cfg), hs, state)
        print(json.dumps(out, indent=2))
        return 0
    if args.command == "commit":
        results = pipeline.commit(cfg, hs, state, only=args.only)
        if not results:
            print("Nothing to commit (no extraction.json files waiting).")
        for r in results:
            print(pipeline.render_summary(r))
            print("\n" + "-" * 60 + "\n")
        return 0
    return 0


READ_SCOPES = ["crm.objects.contacts.read", "crm.objects.companies.read", "crm.objects.deals.read",
               "crm.objects.owners.read", "crm.objects.notes.read", "crm.objects.meetings.read",
               "crm.objects.tasks.read"]


def scope_report(hs, cfg, line):
    """Compare the token's granted scopes with what this skill needs. Informational: the functional
    probes below are what decide PASS/FAIL, because scope names can differ by account/picker."""
    try:
        have = set(hs.token_scopes())
    except Exception as e:  # noqa: BLE001 - endpoint is best-effort
        print(f"INFO  HubSpot: could not read token scopes ({e}); rely on the probes below.")
        return
    write_specific = ["crm.objects.meetings.write", "crm.objects.tasks.write"]
    if cfg["write_as"] == "note":
        write_specific[0] = "crm.objects.notes.write"
    missing_w = [s for s in write_specific if s not in have]
    broad = {"crm.objects.contacts.write", "crm.objects.deals.write"} <= have
    missing_r = [s for s in READ_SCOPES if s not in have]
    print("INFO  HubSpot token scopes: " + (", ".join(sorted(have)) or "(none)"))
    if missing_r:
        print("WARN  HubSpot read scopes not listed: " + ", ".join(missing_r) + " (the read probes below decide)")
    if not missing_w:
        line(True, "HubSpot: specific write scopes present: " + ", ".join(write_specific))
    elif broad:
        print("WARN  HubSpot: using broad contacts.write + deals.write instead of " + ", ".join(missing_w)
              + ". Works, but the token could edit deals/contacts; our code allowlist is then the only guard.")
    else:
        line(False, "HubSpot: missing write scopes: " + ", ".join(missing_w))


def doctor(cfg, problems):
    ok = True

    def line(passed, msg):
        nonlocal ok
        ok = ok and passed
        print(("PASS  " if passed else "FAIL  ") + msg)

    line(not problems, "config + env vars" + ("" if not problems else ": " + "; ".join(problems)))
    if problems:
        return 1
    try:
        z = ZoomClient(cfg)
        z.token()
        line(True, "Zoom: server-to-server OAuth token obtained")
        from datetime import datetime, timedelta, timezone
        d = datetime.now(timezone.utc)
        for rep in rep_emails(cfg):
            try:
                recs = z.list_user_recordings(rep, (d - timedelta(days=2)).strftime("%Y-%m-%d"), d.strftime("%Y-%m-%d"))
                line(True, f"Zoom: can list recordings for {rep} ({len(recs)} in last 2 days, "
                           f"{sum(1 for r in recs if r['transcript_url'])} with transcripts)")
                if recs:
                    try:
                        n = len(z.get_participants(recs[0]["uuid"]))
                        line(True, f"Zoom: can read past-meeting participants ({n} on latest recording)")
                    except ApiError as e:
                        line(False, f"Zoom: participants scope (meeting:read:list_past_participants:admin): {e}")
            except ApiError as e:
                line(False, f"Zoom: recordings for {rep}: {e}")
    except ApiError as e:
        line(False, f"Zoom OAuth: {e}")
    hs = HubSpotClient(cfg)
    scope_report(hs, cfg, line)
    for label, fn in (("notes read", lambda: hs.search("notes", [{"propertyName": "hs_note_body", "operator": "HAS_PROPERTY"}], ["hs_note_body"], 1)),
                      ("meetings read", lambda: hs.search("meetings", [{"propertyName": "hs_meeting_title", "operator": "HAS_PROPERTY"}], ["hs_meeting_title"], 1)),
                      ("tasks read", lambda: hs.search("tasks", [{"propertyName": "hs_task_subject", "operator": "HAS_PROPERTY"}], ["hs_task_subject"], 1)),
                      ("owners", lambda: hs._read("GET", "/crm/v3/owners", params={"limit": 1})),
                      ("contacts search", lambda: hs.search("contacts", [{"propertyName": "email", "operator": "EQ", "value": "nobody@example.invalid"}], ["email"], 1)),
                      ("companies search", lambda: hs.search("companies", [{"propertyName": "domain", "operator": "EQ", "value": "example.invalid"}], ["name"], 1)),
                      ("deals read", lambda: hs.search("deals", [{"propertyName": "dealname", "operator": "CONTAINS_TOKEN", "value": "zzzzzz"}], ["dealname"], 1))):
        try:
            fn()
            line(True, f"HubSpot: {label}")
        except ApiError as e:
            line(False, f"HubSpot {label}: {e}")
    for rep in rep_emails(cfg):
        try:
            oid = hs.owner_id_by_email(rep)
            line(bool(oid), f"HubSpot: owner found for {rep}" if oid else f"HubSpot: no owner with email {rep}")
        except ApiError as e:
            line(False, f"HubSpot owner lookup {rep}: {e}")
    print("\nNote: write scopes (notes/meetings/tasks) are only verified on the first real write; "
          "run `prepare`/`commit` with --dry-run first.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
