# post-call-hubspot (Hermes skill)

Zoom call transcripts -> verified, short HubSpot deal notes + tasks. One shared pipeline for all reps
(no agent or profile per rep). Zoom and HubSpot only.

```
Zoom (S2S OAuth) --> prepare: transcript + attendees -> contact -> company -> latest-updated deal
                         |
                         v   (agent: extraction only, no CRM tools)
                     extraction.json --> commit: verify quotes, cap 5 sentences, allowlisted writes
                         |
        high/medium match: note/meeting + tasks on the deal      low match: HubSpot review task for the rep
```

## What changed vs. the original repo
| Original | Now |
|---|---|
| One Hermes profile + cron per rep | One pipeline; reps are rows in `settings.yaml` (opt-in allowlist) |
| Per-user HubSpot "private app tokens" (not actually per-user) | One HubSpot private app token, env var; attribution via HubSpot owner |
| Emails parsed from the transcript | Emails from Zoom's participants API; internal domains filtered out |
| LLM did matching | Deterministic matching in code; **latest-updated deal** rule |
| Self-reported confidence | Every item needs a verbatim quote that is machine-checked against the transcript |
| Local `pending/` + `.review` files | HubSpot review task assigned to the rep |
| Agent had write tools | Agent only writes `extraction.json`; code does writes via an allowlist (notes, meetings, tasks) |
| Hourly poll, `.processed` markers | Poll (+ optional webhook); idempotent via `pcix-ref` marker in HubSpot |
| Unbounded notes | **5-sentence cap** enforced in code |

## Setup

### 1. Install
Everything lives in one folder: `~/.hermes/skills/post-call-hubspot/`
```bash
unzip post-call-hubspot.zip -d ~/.hermes/skills/        # creates ~/.hermes/skills/post-call-hubspot/
cd ~/.hermes/skills/post-call-hubspot
pip install -r requirements.txt                          # PyYAML only
cp config/settings.example.yaml config/settings.yaml     # then edit
```
Python 3.9+. No other dependencies. Runtime files (work items, `state.db`) are created under `data/` inside
that folder. `config/settings.yaml` and `data/` are local only; do not commit them.

### 2. Zoom (admin, once)
Create a **Server-to-Server OAuth** app (Zoom App Marketplace > Develop > Build App). One app covers all reps.

**Scopes (Scopes tab, granular, the `:admin` variants that S2S apps use):**
| Scope | Used for |
|---|---|
| `cloud_recording:read:list_user_recordings:admin` | list each opted-in rep's cloud recordings |
| `cloud_recording:read:list_recording_files:admin` | get a meeting's recording files (webhook-queued meetings) |
| `meeting:read:list_past_participants:admin` | attendee emails for matching |

**Also required in Zoom (not scopes):**
- The admin who creates the app needs a role with **"Server-to-server Auth app"** and **"View the recording and transcript content"** permissions.
- Plan: **Pro or higher**. Cloud recording and **Audio transcript** must be enabled for each rep
  (Settings > Recording). No transcript file = nothing to process.
- Webhook (optional): add the *Recording Transcript Completed* event under Feature > Event Subscriptions and
  copy the Secret Token to `ZOOM_WEBHOOK_SECRET_TOKEN`.

### 3. HubSpot (super admin, once)
Create a **Private App** (Settings > Integrations > Private Apps; requires super admin).

| Scope | Used for |
|---|---|
| `crm.objects.contacts.read`, `crm.objects.companies.read`, `crm.objects.deals.read` | find contact -> company -> deals |
| `crm.objects.owners.read` | map the host rep to a HubSpot owner |
| `crm.objects.notes.read`, `crm.objects.meetings.read`, `crm.objects.tasks.read` | dedupe marker, call numbering, review tasks |
| **`crm.objects.meetings.write`**, **`crm.objects.tasks.write`** | create the call meeting + tasks (default `write_as: meeting`) |
| `crm.objects.notes.write` | only if you set `write_as: note` |

**Least privilege vs. fallback.** Prefer the specific `*.meetings/tasks/notes.write` scopes above: they let the
token create activities but not edit your deals or contacts. If your scope picker doesn't offer them, the
commonly documented fallback is `crm.objects.contacts.write` + `crm.objects.deals.write`. That works, but it
is broader than this skill needs (the token could then edit deals), so the code-level write allowlist becomes
the only guard. If a write ever returns `403 MISSING_SCOPES`, HubSpot's error names the scope to add.

**Verify:** `python3 scripts/pcix.py doctor` reads your token's granted scopes (HubSpot's access-token-info
endpoint), compares them with this table, and probes read access to every object we use. Write access can only be
proven by a real write: after `doctor`, run one low-risk real cycle with a single test rep.

### 4. Secrets (environment variables; never in the config file)
```
HUBSPOT_TOKEN=...
ZOOM_ACCOUNT_ID=...  ZOOM_CLIENT_ID=...  ZOOM_CLIENT_SECRET=...
ZOOM_WEBHOOK_SECRET_TOKEN=...      # only if you run the webhook receiver
```

### 5. Configure + verify
Edit `config/settings.yaml`: `internal_domains`, `reps` (default-deny: only listed reps' meetings are read),
`write_as` (default `meeting`). Then:
```bash
python3 scripts/pcix.py doctor              # config, Zoom token, HubSpot reads, owner per rep
python3 scripts/pcix.py prepare --dry-run   # see what would be matched; no writes
```
Run the unit tests any time: `python3 -m unittest discover -s tests`.

### 6. Schedule it
```bash
hermes cron create "every 15 minutes" "Run one cycle of the post-call-hubspot skill." \
  --name post-call-intel --deliver local --skill post-call-hubspot
```
(Flags are the ones used in the original repo; adjust to your Hermes version. One job for everyone.)

### 7. Optional: Zoom webhook for near-real-time
`python3 scripts/pcix.py webhook` listens on `webhook.host:port/path`, verifies Zoom's signature, and queues the
meeting. Expose it over HTTPS (reverse proxy), subscribe the Zoom app to the *recording transcript completed*
event, and set `webhook.trigger_command` to a command that runs one skill cycle (or just keep the cron as the
trigger; a queued meeting is picked up on the next cycle). Polling still runs, so a missed webhook costs nothing.

## Reps' daily experience
Nothing. Notes appear on the deal. If the match was uncertain they get one HubSpot task:
**complete it** to approve the suggested deal, **edit the `DEAL_ID:` line** first to choose another, or put
**`DISMISS`** on its own line to drop it. Review tasks expire after 14 days.

## Rules worth knowing
- **Client with several opportunities -> the most recently updated one** (`hs_lastmodifieddate`), closed deals
  included. To ignore closed deals when choosing, set `deal_selection.include_closed: false`.
- **Meeting (default) or note bodies <= 5 sentences** of prose, plus a one-line footer (date, call #, match confidence,
  `pcix-ref` marker) that is metadata, not prose. Tasks: short title, one-line body, only for *our* next steps.
- Call number = how many calls this skill already logged on that deal + 1. Backfill by raising
  `lookback_hours` before the first run; meetings are processed oldest first.
- Known limits: guests who join Zoom without signing in have no email, so they can't be matched by email
  (the call falls back to topic matching = review task). If the note is created but a task creation fails,
  tasks are not retried (the error is shown in the output).

## Data retention and access (recommended defaults, all configurable)
| Data | Where | Default |
|---|---|---|
| Transcript text (`transcript.txt`) | `data/work/<id>/` | **Deleted immediately** after the call is written/skipped/sent to review |
| `context.json` + `extraction.json` (attendee emails, short quotes) | `data/work/<id>/` | Deleted **7 days** after the call is finished (`retention_days`) |
| `state.db` (ids, statuses, proposed review text) | `data/state.db` | Rows deleted after **90 days** (`state_retention_days`); open reviews kept |
| The recording + transcript itself | Zoom | Governed by Zoom retention: set Zoom's own auto-delete to match your policy |
| What reaches HubSpot | CRM | <= 5 sentences + short tasks, under HubSpot's normal permissions |

`prepare` purges automatically each cycle; run `python3 scripts/pcix.py purge` (or `--dry-run`) manually any time.
Everything the skill creates is owner-only (umask 077).

**Who should have access to the host:** run it on a dedicated machine/VM or service account, limited to 2-3 named
Sales Ops/IT admins; full-disk encryption on; keep `data/` out of backups and git; keep secrets in environment
variables or a secret manager (not in `settings.yaml`, not in shell history); rotate the HubSpot and Zoom
credentials if an admin leaves.

## Files
`SKILL.md` agent instructions | `prompts/extract.md` extraction prompt | `scripts/` CLI + library |
`config/settings.example.yaml` | `examples/` sample transcripts + expected output | `tests/` | `PRD.md` | `docs/post-call-hubspot-overview.pdf` (plain-language overview)

Not verified against live accounts: this package was tested with in-memory fakes of the Zoom and HubSpot APIs,
not live endpoints. Run `doctor` and a `--dry-run` cycle before enabling the cron.
