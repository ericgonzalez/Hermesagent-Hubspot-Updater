"""Configuration loading. Secrets come from environment variables only."""
import copy
import json
import os

try:
    import yaml  # type: ignore
except ImportError:  # JSON config still works without PyYAML
    yaml = None

# <...>/skills/post-call-hubspot  (this file is scripts/pcix_lib/config.py)
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULTS = {
    # Your own company's email domains. Attendees on these domains are "internal".
    "internal_domains": [],
    # Opt-in allowlist of reps whose Zoom recordings may be processed.
    # Each: {"email": "rep@yourco.com"}. Default-deny: empty list = nothing is processed.
    "reps": [],
    "lookback_hours": 48,
    "min_transcript_words": 150,
    # "meeting" (default) or "note": which HubSpot object to create on the deal.
    "write_as": "meeting",
    "max_note_sentences": 5,
    "create_tasks": True,
    "max_tasks_per_call": 3,
    # Deal selection rule when a client (company) has several deals.
    "deal_selection": {"rule": "latest_updated", "include_closed": True},
    "review_task_prefix": "[Call Intel Review]",
    "review_expiry_days": 14,
    "max_prepare_attempts": 3,
    "work_dir": os.path.join(SKILL_DIR, "data", "work"),
    "state_db": os.path.join(SKILL_DIR, "data", "state.db"),
    # Retention of local files (transcripts are the sensitive part).
    "delete_transcript_after_commit": True,   # transcript.txt removed as soon as the item is finished
    "retention_days": 7,                      # finished work folders (context/extraction) deleted after N days
    "state_retention_days": 90,               # old rows in state.db deleted after N days
    "dry_run": False,
    "hubspot": {
        "base_url": "https://api.hubapi.com",
        # HUBSPOT_DEFINED association type ids (override only if HubSpot changes them)
        "assoc_ids": {"note_to_deal": 214, "meeting_to_deal": 212, "task_to_deal": 216},
    },
    "zoom": {
        "api_base": "https://api.zoom.us/v2",
        "oauth_url": "https://zoom.us/oauth/token",
    },
    "webhook": {
        "host": "127.0.0.1",
        "port": 8787,
        "path": "/zoom/webhook",
        # Optional shell command run (non-blocking) after a job is queued, e.g. to kick the Hermes skill.
        "trigger_command": "",
    },
}

REQUIRED_ENV_ZOOM = ["ZOOM_ACCOUNT_ID", "ZOOM_CLIENT_ID", "ZOOM_CLIENT_SECRET"]
REQUIRED_ENV_HUBSPOT = ["HUBSPOT_TOKEN"]


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def expand(path):
    return os.path.abspath(os.path.expanduser(path))


def default_config_path():
    """PCIX_CONFIG, else <skill dir>/config/settings.yaml (e.g. ~/.hermes/skills/post-call-hubspot/config/)."""
    if os.environ.get("PCIX_CONFIG"):
        return expand(os.environ["PCIX_CONFIG"])
    return os.path.join(SKILL_DIR, "config", "settings.yaml")


def load_config(path=None, overrides=None):
    path = expand(path) if path else default_config_path()
    data = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        if path.endswith(".json"):
            data = json.loads(text)
        else:
            if yaml is None:
                raise RuntimeError("PyYAML is required for YAML config (pip install pyyaml) or use a .json config.")
            data = yaml.safe_load(text) or {}
    cfg = _merge(DEFAULTS, data)
    cfg = _merge(cfg, overrides or {})
    cfg["_path"] = path
    cfg["internal_domains"] = [d.lower().lstrip("@") for d in cfg["internal_domains"]]
    cfg["work_dir"] = expand(cfg["work_dir"])
    cfg["state_db"] = expand(cfg["state_db"])
    return cfg


def validate_config(cfg, need_zoom=True, need_hubspot=True):
    """Return a list of human-readable problems (empty list = OK)."""
    problems = []
    if not cfg["internal_domains"]:
        problems.append("internal_domains is empty: without it every attendee looks external.")
    if not cfg["reps"]:
        problems.append("reps is empty: nothing will be processed (default-deny). Add at least one rep email.")
    for r in cfg["reps"]:
        if not isinstance(r, dict) or "@" not in str(r.get("email", "")):
            problems.append(f"reps entry needs an 'email': {r!r}")
    if cfg["write_as"] not in ("note", "meeting"):
        problems.append("write_as must be 'note' or 'meeting'.")
    if not (1 <= int(cfg["max_note_sentences"]) <= 5):
        problems.append("max_note_sentences must be between 1 and 5 (policy cap is 5).")
    if int(cfg["retention_days"]) < 1 or int(cfg["state_retention_days"]) < 1:
        problems.append("retention_days and state_retention_days must be >= 1.")
    if cfg["deal_selection"].get("rule") != "latest_updated":
        problems.append("deal_selection.rule must be 'latest_updated'.")
    if need_zoom:
        problems += [f"missing env var {v}" for v in REQUIRED_ENV_ZOOM if not os.environ.get(v)]
    if need_hubspot:
        problems += [f"missing env var {v}" for v in REQUIRED_ENV_HUBSPOT if not os.environ.get(v)]
    return problems


def rep_emails(cfg):
    return [r["email"].lower() for r in cfg["reps"] if isinstance(r, dict) and r.get("email")]
