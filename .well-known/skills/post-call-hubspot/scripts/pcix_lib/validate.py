"""Validates the agent's extraction.json against the real transcript before anything is written.
Rules: verbatim quote must exist; low confidence is withheld; no URLs/HTML; enum checks."""
import re

from .transcript import normalize

CONF_OK = {"high", "medium"}
URL_RE = re.compile(r"(https?://|www\.|<\s*/?\s*[a-z][^>]*>)", re.I)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SENTIMENT = {"positive", "stalled", "at_risk", "unclear"}
SIDES = {"us", "them"}
ROLES = {"decision_maker", "influencer", "blocker", "unknown"}
MAX_TEXT, MAX_QUOTE, MIN_QUOTE = 400, 300, 12


def _clean(s, limit):
    s = " ".join(str(s or "").split())
    return s[:limit]


def _check_item(item, corpus_norm, require_text=True):
    """Return (ok, reason)."""
    if not isinstance(item, dict):
        return False, "malformed"
    text, quote = _clean(item.get("text"), MAX_TEXT), _clean(item.get("quote"), MAX_QUOTE)
    if require_text and not text:
        return False, "malformed"
    if URL_RE.search(text) or URL_RE.search(quote):
        return False, "unsafe_text"
    conf = str(item.get("confidence", "low")).lower()
    if conf not in CONF_OK:
        return False, "low_confidence"
    nq = normalize(quote)
    if len(nq) < MIN_QUOTE or nq not in corpus_norm:
        return False, "quote_not_found"
    return True, ""


def validate_extraction(raw, corpus, cfg=None):
    """Return {'clean': {...}, 'withheld': [...], 'errors': [...]}"""
    out = {"clean": {"note_sentences": [], "objections": [], "timeline_signals": [], "next_steps": [],
                     "sentiment": None, "contact_roles": []},
           "withheld": [], "errors": []}
    if not isinstance(raw, dict):
        out["errors"].append("extraction is not a JSON object")
        return out
    corpus_norm = normalize(corpus)

    def go(field, extra=None):
        for item in raw.get(field) or []:
            ok, why = _check_item(item, corpus_norm)
            if ok and extra:
                ok, why = extra(item)
            label = _clean(item.get("text"), 80) if isinstance(item, dict) else str(item)[:80]
            if ok:
                kept = {"text": _clean(item["text"], MAX_TEXT), "quote": _clean(item["quote"], MAX_QUOTE),
                        "confidence": str(item["confidence"]).lower()}
                for k in ("speaker", "owner_side", "owner_name", "due", "role", "name", "email"):
                    if k in item:
                        kept[k] = _clean(item[k], 120) if item[k] is not None else None
                out["clean"][field].append(kept)
            else:
                out["withheld"].append({"field": field, "text": label, "reason": why})

    def next_step_extra(i):
        if str(i.get("owner_side", "")).lower() not in SIDES:
            return False, "bad_owner_side"
        if i.get("due") not in (None, "") and not DATE_RE.match(str(i.get("due"))):
            return False, "bad_due_date"
        return True, ""

    def role_extra(i):
        return (str(i.get("role", "")).lower() in ROLES), "bad_role"

    go("note_sentences")
    go("objections")
    go("timeline_signals")
    go("next_steps", next_step_extra)
    go("contact_roles", role_extra)
    for s in out["clean"]["next_steps"]:
        s["owner_side"] = s["owner_side"].lower()
        s["due"] = s.get("due") or None

    sent = raw.get("sentiment")
    if isinstance(sent, dict):
        sent = {**sent, "text": sent.get("value", "")}
        ok, why = _check_item(sent, corpus_norm, require_text=False)
        val = str(sent.get("value", "")).lower()
        if ok and val in SENTIMENT and val != "unclear":
            out["clean"]["sentiment"] = {"value": val, "quote": _clean(sent["quote"], MAX_QUOTE),
                                         "confidence": str(sent["confidence"]).lower()}
        else:
            out["withheld"].append({"field": "sentiment", "text": val, "reason": why or "unclear_or_invalid"})
    return out
