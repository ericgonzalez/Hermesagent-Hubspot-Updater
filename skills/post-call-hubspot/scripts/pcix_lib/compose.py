"""Builds HubSpot-bound text. Hard cap: max N sentences of prose per note/meeting."""
import html
import re

_PROTECT = ["e.g.", "i.e.", "vs.", "Mr.", "Mrs.", "Ms.", "Dr.", "Prof."]
_DOT = "\u2024"  # one-dot leader, stands in for protected periods
_SENT = re.compile(r"""(.+?(?:[.!?]+["')\]]*(?=\s+["'(\[]?[A-Z0-9])|$))""", re.S)
MARK = "pcix-ref:"


def split_sentences(text):
    """Conservative splitter: when ambiguous it over-splits, so the cap errs on the strict side."""
    t = " ".join((text or "").split())
    if not t:
        return []
    for a in _PROTECT:
        t = t.replace(a, a.replace(".", _DOT))
    return [s.strip().replace(_DOT, ".") for s in _SENT.findall(t) if s.strip()]


def count_sentences(text):
    return len(split_sentences(text))


def cap_sentences(texts, max_n):
    """Join the (priority-ordered) texts, re-split, keep the first max_n sentences."""
    sents = split_sentences(" ".join(texts))
    return sents[:max_n], len(sents) > max_n


def footer(meta, flags=None):
    parts = [f"Zoom call {meta['date']}", f"#{meta['call_number']}", f"match: {meta['match_label']}"]
    if flags:
        parts.append(flags)
    parts.append(f"{MARK}{meta['uuid']}")
    return " | ".join(parts)


def note_html(sentences, meta, flags=None):
    prose = " ".join(sentences)
    return f"<p>{html.escape(prose)}</p><p><small>{html.escape(footer(meta, flags))}</small></p>"


def plain_prose(sentences):
    return " ".join(sentences)


def find_refs(body):
    return set(re.findall(re.escape(MARK) + r"([^\s<|]+)", html.unescape(body or "")))


def footer_date(body):
    m = re.search(r"Zoom call (\d{4}-\d{2}-\d{2})", html.unescape(body or ""))
    return m.group(1) if m else None
