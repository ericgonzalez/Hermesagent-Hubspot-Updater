import glob
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from pcix_lib import compose, webhook  # noqa: E402
from pcix_lib.hubspot import HubSpotClient, html_to_text  # noqa: E402
from pcix_lib.config import load_config  # noqa: E402
from pcix_lib.matching import select_deal  # noqa: E402
from pcix_lib.transcript import corpus_from_rendered, parse_vtt, render_numbered  # noqa: E402
from pcix_lib.validate import validate_extraction  # noqa: E402


def rd(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def jload(path):
    return json.loads(rd(path))


def wr(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def corpus_for(name):
    segs = parse_vtt(rd(os.path.join(ROOT, "examples", "transcripts", name + ".vtt")))
    return corpus_from_rendered(render_numbered(segs))


class SentenceCap(unittest.TestCase):
    def test_cap_never_exceeds_five(self):
        samples = [
            "One. Two. Three. Four. Five. Six. Seven.",
            "Acme wants a pilot, e.g. a Salesforce sync. Dr. Lee approves Friday! Budget is $1.5M... Next is legal? Yes. Sixth. Seventh.",
            "A single long run-on sentence without any terminal punctuation at all",
            "Question? Answer! Statement. " * 6,
        ]
        for s in samples:
            kept, _ = compose.cap_sentences([s], 5)
            self.assertLessEqual(compose.count_sentences(" ".join(kept)), 5, s)

    def test_footer_not_counted_and_marker_roundtrip(self):
        meta = {"date": "2026-10-02", "call_number": 3, "match_label": "high", "uuid": "abc/def=="}
        html = compose.note_html(["A. B. C."], meta)
        self.assertEqual(compose.find_refs(html), {"abc/def=="})
        self.assertEqual(compose.footer_date(html), "2026-10-02")


class Examples(unittest.TestCase):
    def test_expected_extractions_validate(self):
        for path in glob.glob(os.path.join(ROOT, "examples", "expected", "*.json")):
            name = os.path.basename(path)[:-5]
            raw = jload(path)
            v = validate_extraction(raw, corpus_for(name))
            self.assertEqual(v["errors"], [], name)
            kept, _ = compose.cap_sentences([s["text"] for s in v["clean"]["note_sentences"]], 5)
            self.assertLessEqual(len(kept), 5, name)
            self.assertGreater(len(kept), 0, name)

    def test_noisy_low_confidence_is_withheld(self):
        raw = jload(os.path.join(ROOT, "examples", "expected", "initech_noisy.json"))
        v = validate_extraction(raw, corpus_for("initech_noisy"))
        reasons = {(w["field"], w["reason"]) for w in v["withheld"]}
        self.assertIn(("note_sentences", "low_confidence"), reasons)


class ValidatorGuards(unittest.TestCase):
    corpus = "we want to sign before the end of november and the budget is approved for this quarter"

    def _one(self, **item):
        base = {"text": "Wants to sign by November.", "quote": "we want to sign before the end of november", "confidence": "high"}
        base.update(item)
        return validate_extraction({"note_sentences": [base]}, self.corpus)

    def test_ok(self):
        self.assertEqual(len(self._one()["clean"]["note_sentences"]), 1)

    def test_fabricated_quote_dropped(self):
        v = self._one(quote="the customer agreed to a fifty percent discount")
        self.assertEqual(v["clean"]["note_sentences"], [])
        self.assertEqual(v["withheld"][0]["reason"], "quote_not_found")

    def test_urls_and_html_dropped(self):
        self.assertEqual(self._one(text="See http://evil.example/x")["withheld"][0]["reason"], "unsafe_text")
        self.assertEqual(self._one(text="<b>bold</b> claim")["withheld"][0]["reason"], "unsafe_text")

    def test_bad_input(self):
        self.assertTrue(validate_extraction("nope", self.corpus)["errors"])


class Deals(unittest.TestCase):
    def test_latest_updated_wins(self):
        deals = [
            {"id": "1", "properties": {"dealname": "Old", "hs_lastmodifieddate": "2026-01-01T00:00:00Z"}},
            {"id": "2", "properties": {"dealname": "Newest", "hs_lastmodifieddate": "2026-09-30T10:00:00.123Z"}},
            {"id": "3", "properties": {"dealname": "Mid", "hs_lastmodifieddate": "2026-06-01T00:00:00Z"}},
        ]
        d, n = select_deal(deals)
        self.assertEqual((d["id"], n), ("2", 3))

    def test_exclude_closed_option(self):
        deals = [
            {"id": "1", "properties": {"dealname": "Closed new", "hs_lastmodifieddate": "2026-09-30T00:00:00Z", "hs_is_closed": "true"}},
            {"id": "2", "properties": {"dealname": "Open old", "hs_lastmodifieddate": "2026-01-01T00:00:00Z", "hs_is_closed": "false"}},
        ]
        self.assertEqual(select_deal(deals, include_closed=False)[0]["id"], "2")
        self.assertEqual(select_deal(deals, include_closed=True)[0]["id"], "1")


class Safety(unittest.TestCase):
    def test_no_deal_or_stage_writes_possible(self):
        hs = HubSpotClient(load_config("/nonexistent.json"), env={"HUBSPOT_TOKEN": "x"}, dry_run=True)
        for path in ("/crm/v3/objects/deals/123", "/crm/v3/objects/deals", "/crm/v3/objects/contacts"):
            with self.assertRaises(PermissionError):
                hs._write(path, {"properties": {"amount": "0"}})
        self.assertFalse(any("update" in n for n in dir(hs)))

    def test_html_task_body_parsing(self):
        t = html_to_text("<div>DEAL_ID: 555</div><div>PCIX_MEETING: u</div><p>DISMISS</p>")
        self.assertIn("DEAL_ID: 555", t.splitlines())


class Webhook(unittest.TestCase):
    def test_signature(self):
        body, ts, secret = '{"a":1}', "1000", "s3cret"
        sig = "v0=" + webhook.sign(secret, f"v0:{ts}:{body}")
        self.assertTrue(webhook.verify(secret, ts, body, sig, now=1100))
        self.assertFalse(webhook.verify(secret, ts, body, sig, now=99999))
        self.assertFalse(webhook.verify(secret, ts, body + "x", sig, now=1100))

    def test_url_validation(self):
        os.environ["ZOOM_WEBHOOK_SECRET_TOKEN"] = "s3cret"
        code, resp = webhook.handle_event({"reps": []}, None, {"event": "endpoint.url_validation", "payload": {"plainToken": "abc"}})
        self.assertEqual(resp["encryptedToken"], webhook.sign("s3cret", "abc"))


if __name__ == "__main__":
    unittest.main()
