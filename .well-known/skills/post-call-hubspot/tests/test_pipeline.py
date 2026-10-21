import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fakes import FakeHubSpot, FakeZoom  # noqa: E402
from pcix_lib import compose, pipeline  # noqa: E402
from pcix_lib.config import load_config  # noqa: E402
from pcix_lib.state import State  # noqa: E402


def rd(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def jload(path):
    return json.loads(rd(path))


def wr(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def vtt(name):
    return rd(os.path.join(ROOT, "examples", "transcripts", name + ".vtt"))


def expected(name):
    return rd(os.path.join(ROOT, "examples", "expected", name + ".json"))


class Flow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = load_config("/nonexistent.json", overrides={
            "internal_domains": ["yourco.com"], "reps": [{"email": "alex@yourco.com"}],
            "work_dir": os.path.join(self.tmp, "work"), "state_db": ":memory:", "min_transcript_words": 50})
        self.state = State(":memory:")
        hs = self.hs = FakeHubSpot()
        hs.owners["alex@yourco.com"] = "77"
        hs.contacts["dana@acme.com"] = {"id": "c1", "company": "co1"}
        hs.companies["co1"] = {"name": "Acme", "domain": "acme.com", "deals": ["d1", "d2", "d3"]}
        hs.deals["d1"] = {"dealname": "Acme - Old pilot", "hs_lastmodifieddate": "2026-02-01T00:00:00Z"}
        hs.deals["d2"] = {"dealname": "Acme - Expansion", "hs_lastmodifieddate": "2026-09-29T10:00:00Z", "hubspot_owner_id": "99"}
        hs.deals["d3"] = {"dealname": "Acme - Renewal", "hs_lastmodifieddate": "2026-07-01T00:00:00Z"}
        self.meetings = [
            {"uuid": "u-acme", "id": "1", "topic": "Acme follow-up", "start_time": "2026-10-02T15:00:00Z", "duration": 30,
             "host_email": "alex@yourco.com", "transcript_url": "t-acme"},
            {"uuid": "u-globex", "id": "2", "topic": "Globex check in", "start_time": "2026-10-03T15:00:00Z", "duration": 30,
             "host_email": "alex@yourco.com", "transcript_url": "t-globex"},
            {"uuid": "u-internal", "id": "3", "topic": "Team sync", "start_time": "2026-10-03T18:00:00Z", "duration": 30,
             "host_email": "alex@yourco.com", "transcript_url": "t-int"},
        ]
        parts = {
            "u-acme": [{"name": "Alex", "email": "alex@yourco.com"}, {"name": "Dana", "email": "dana@acme.com"}],
            "u-globex": [{"name": "Alex", "email": "alex@yourco.com"}, {"name": "Marcus", "email": ""}],
            "u-internal": [{"name": "Alex", "email": "alex@yourco.com"}, {"name": "Bo", "email": "bo@yourco.com"}],
        }
        self.zoom = FakeZoom(self.meetings, {"t-acme": vtt("acme_followup"), "t-globex": vtt("globex_stalled"),
                                             "t-int": vtt("initech_noisy")}, parts)
        hs.deals["d9"] = {"dealname": "Globex - Platform", "hs_lastmodifieddate": "2026-08-01T00:00:00Z"}

    def eng(self):
        """Created notes/meetings (the default write_as is meeting)."""
        return [c for c in self.hs.created if c["kind"] in ("note", "meeting")]

    def agent_writes(self, pending, mapping):
        for p in pending:
            ctx = jload(p["context"])
            name = mapping.get(ctx["meeting"]["uuid"])
            if name:
                wr(p["write_extraction_to"], expected(name))

    def test_high_confidence_writes_latest_updated_deal_with_cap_and_tasks(self):
        out = pipeline.prepare(self.cfg, self.zoom, self.hs, self.state)
        self.assertEqual({s["reason"] for s in out["skipped"]}, {"internal_only"})
        self.agent_writes(out["pending"], {"u-acme": "acme_followup"})
        results = pipeline.commit(self.cfg, self.hs, self.state)
        r = next(x for x in results if x["meeting"] == "u-acme")
        self.assertEqual(r["status"], "written")
        self.assertEqual(r["match"]["deal"]["id"], "d2")                 # latest updated of 3
        self.assertEqual(r["match"]["selected_by"], "latest_updated")
        notes = self.eng()
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["kind"], "meeting")                     # default object type
        self.assertEqual(notes[0]["deal"], "d2")
        self.assertEqual(notes[0]["owner"], "77")                         # host rep's HubSpot owner
        prose = notes[0]["body"].split("</p>")[0]
        self.assertLessEqual(compose.count_sentences(prose.replace("<p>", "")), 5)
        self.assertIn("match: high", notes[0]["body"])
        tasks = [c for c in self.hs.created if c["kind"] == "task"]
        self.assertEqual(len(tasks), 2)                                   # only OUR next steps

    def test_idempotent_rerun(self):
        out = pipeline.prepare(self.cfg, self.zoom, self.hs, self.state)
        self.agent_writes(out["pending"], {"u-acme": "acme_followup"})
        pipeline.commit(self.cfg, self.hs, self.state)
        again = pipeline.prepare(self.cfg, self.zoom, self.hs, self.state)
        self.assertFalse([p for p in again["pending"] if "acme" in p["context"]])
        fresh = State(":memory:")                                         # even with lost local state...
        out3 = pipeline.prepare(self.cfg, self.zoom, self.hs, fresh)
        self.assertIn("already_in_hubspot", {s["reason"] for s in out3["skipped"]})
        self.assertEqual(len(self.eng()), 1)

    def test_note_mode(self):
        self.cfg["write_as"] = "note"
        out = pipeline.prepare(self.cfg, self.zoom, self.hs, self.state)
        self.agent_writes(out["pending"], {"u-acme": "acme_followup"})
        pipeline.commit(self.cfg, self.hs, self.state)
        self.assertEqual([c["kind"] for c in self.eng()], ["note"])

    def test_closed_deals_are_eligible_by_default(self):
        self.hs.deals["d2"]["hs_is_closed"] = "true"                      # latest-updated deal is closed
        out = pipeline.prepare(self.cfg, self.zoom, self.hs, self.state)
        self.agent_writes(out["pending"], {"u-acme": "acme_followup"})
        r = [x for x in pipeline.commit(self.cfg, self.hs, self.state) if x["meeting"] == "u-acme"][0]
        self.assertEqual(r["match"]["deal"]["id"], "d2")

    def test_transcript_deleted_after_commit_and_purged_later(self):
        out = pipeline.prepare(self.cfg, self.zoom, self.hs, self.state)
        self.agent_writes(out["pending"], {"u-acme": "acme_followup"})
        item = next(p for p in out["pending"] if "u-acme" in rd(p["context"]))
        pipeline.commit(self.cfg, self.hs, self.state)
        self.assertFalse(os.path.exists(item["transcript"]))              # gone right after commit
        self.assertTrue(os.path.exists(item["context"]))                  # kept for audit until retention
        wd = os.path.dirname(item["context"])
        self.assertEqual(pipeline.purge(self.cfg, self.state)["work_dirs_removed"], 0)   # too fresh
        self.state.db.execute("UPDATE items SET updated=updated-? WHERE uuid='u-acme'", (8 * 86400,))
        self.state.db.commit()
        self.assertEqual(pipeline.purge(self.cfg, self.state, dry_run=True)["work_dirs_removed"], 1)
        self.assertTrue(os.path.isdir(wd))                                # dry run deletes nothing
        self.assertEqual(pipeline.purge(self.cfg, self.state)["work_dirs_removed"], 1)
        self.assertFalse(os.path.isdir(wd))

    def test_purge_never_deletes_outside_work_dir(self):
        outside = os.path.join(self.tmp, "keep_me")
        os.makedirs(outside)
        self.state.set_item("evil", "committed", work_dir=outside)
        self.state.db.execute("UPDATE items SET updated=0 WHERE uuid='evil'")
        pipeline.purge(self.cfg, self.state)
        self.assertTrue(os.path.isdir(outside))

    def test_low_confidence_goes_to_review_then_rep_approves(self):
        out = pipeline.prepare(self.cfg, self.zoom, self.hs, self.state)   # Globex: no email, topic-only
        self.agent_writes(out["pending"], {"u-globex": "globex_stalled"})
        results = pipeline.commit(self.cfg, self.hs, self.state)
        r = next(x for x in results if x["meeting"] == "u-globex")
        self.assertEqual(r["status"], "review_task_created")
        self.assertEqual(self.eng(), [])   # nothing written yet
        tid = r["review_task_id"]
        self.assertIn("Suggested deal: Globex - Platform", self.hs.tasks[tid]["body"])
        # rep fixes nothing, just completes the task (HubSpot stores UI edits as HTML)
        self.hs.tasks[tid]["status"] = "COMPLETED"
        self.hs.tasks[tid]["body"] = "<div>DEAL_ID: d9</div>"
        res = pipeline.resolve_reviews(self.cfg, self.hs, self.state)
        self.assertIn("approved", res[0]["result"])
        notes = self.eng()
        self.assertEqual(notes[0]["deal"], "d9")
        self.assertIn("confirmed by rep", notes[0]["body"])
        self.assertEqual(pipeline.resolve_reviews(self.cfg, self.hs, self.state), [])  # not re-applied

    def test_review_dismiss(self):
        out = pipeline.prepare(self.cfg, self.zoom, self.hs, self.state)
        self.agent_writes(out["pending"], {"u-globex": "globex_stalled"})
        r = [x for x in pipeline.commit(self.cfg, self.hs, self.state) if x["meeting"] == "u-globex"][0]
        self.hs.tasks[r["review_task_id"]].update(status="COMPLETED", body="DEAL_ID: d9\nDISMISS")
        self.assertEqual(pipeline.resolve_reviews(self.cfg, self.hs, self.state)[0]["result"], "dismissed")
        self.assertEqual(self.eng(), [])

    def test_not_allowlisted_rep_is_never_listed(self):
        self.cfg["reps"] = [{"email": "someone.else@yourco.com"}]
        out = pipeline.prepare(self.cfg, self.zoom, self.hs, self.state)
        self.assertEqual(out["pending"], [])

    def test_fabricated_extraction_writes_nothing(self):
        out = pipeline.prepare(self.cfg, self.zoom, self.hs, self.state)
        p = next(x for x in out["pending"] if "u-acme" in rd(x["context"]))
        wr(p["write_extraction_to"], json.dumps({"note_sentences": [{"text": "They agreed to a 50 percent discount.", "quote": "we agree to a fifty percent discount today", "confidence": "high"}]}))
        r = [x for x in pipeline.commit(self.cfg, self.hs, self.state) if x["meeting"] == "u-acme"][0]
        self.assertEqual(r["status"], "skipped_no_verified_content")
        self.assertEqual(self.eng(), [])

    def test_summary_renders(self):
        out = pipeline.prepare(self.cfg, self.zoom, self.hs, self.state)
        self.agent_writes(out["pending"], {"u-acme": "acme_followup"})
        r = pipeline.commit(self.cfg, self.hs, self.state)[0]
        text = pipeline.render_summary(r)
        self.assertIn("latest-updated rule among 3", text)


if __name__ == "__main__":
    unittest.main()
