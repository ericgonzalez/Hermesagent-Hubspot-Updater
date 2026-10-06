# PRD: Post-Call Intel (Zoom -> HubSpot)

| | |
|---|---|
| **Status** | v1.1: open questions resolved; ready for pilot |
| **Owner** | Sales Ops (Eric Gonzalez) |
| **Platform** | Hermes agent skill; Zoom + HubSpot only |
| **Supersedes** | `ericgonzalez/Hermesagent-Hubspot-Updater` v0 (per-rep agents) |

## 1. Problem
Reps don't reliably log what happened on calls. CRM records go stale, managers lack deal context, and
follow-up tasks are forgotten. The v0 skill attempted to fix this but had design problems: an agent/profile/cron per
rep, credentials that weren't truly per-user, deal matching that relied on data transcripts don't contain
(emails), self-reported confidence, an agent that held CRM write tools while reading untrusted transcripts,
and unbounded note length.

## 2. Goals
1. After every recorded external sales call, the correct HubSpot opportunity gets a **short** update
   (<= 5 sentences) with no rep effort.
2. **Right record or no record:** uncertain matches never write; they ask the rep via a HubSpot task.
3. One shared pipeline for all reps: no per-rep agents, profiles, crons or tokens.
4. Every written claim is traceable to a verbatim transcript quote, machine-verified.
5. The system cannot alter deal stage, amount, or any record property, even if manipulated.

## 3. Non-goals
- No integrations beyond Zoom and HubSpot (no Slack, email, calendar, other CRMs, other call tools).
- No deal stage/amount/forecast updates; no contact or company creation or editing.
- No call coaching, scoring or analytics; no video/audio analysis (transcript only).
- No handling of calls without a Zoom cloud transcript.

## 4. Users
- **Sales reps** (primary): want zero admin; only touch the system to resolve an occasional review task.
- **Sales managers**: read the deal timeline for current context.
- **Sales Ops/admin**: install once, manage the rep allowlist, monitor failures.

## 5. User stories
- As a rep, my call shows up on the right deal minutes after Zoom finishes the transcript.
- As a rep, if the system isn't sure which deal it was, I get one HubSpot task to confirm, change or dismiss it.
- As a manager, I can read a call note in under 20 seconds and see how many calls came before it.
- As an admin, I can add a rep by adding one line of config and verify with one command.
- As an admin, I can prove the system never edited deal properties and never wrote an unverifiable claim.

## 6. Functional requirements

### Ingestion
- **FR-1** Run on a schedule (default 15 min, polling Zoom cloud recordings with transcripts within a lookback
  window, default 48 h) for **allowlisted reps only** (default-deny).
- **FR-2** Optionally accept Zoom webhooks (signature-verified, replay-window-checked) that queue a meeting;
  polling remains the safety net.
- **FR-3** Skip: no transcript, transcript < 150 words (configurable), internal-only meetings.
- **FR-4** Process oldest meetings first.

### Matching (deterministic, in code)
- **FR-5** Attendee emails come from Zoom's past-meeting participants data, not from transcript text.
- **FR-6** Exclude the host and any attendee on `internal_domains`.
- **FR-7** Resolve attendees -> HubSpot contact (exact email) -> client (company). If no contact, match the
  attendee's email domain to a company's `domain` (free-mail domains ignored).
- **FR-8** **Opportunity rule: if the client has more than one opportunity (deal), always select the
  most recently updated one** (`hs_lastmodifieddate`). **Closed deals are eligible** (decided); an optional setting can exclude them.
- **FR-9** Match confidence: **High** = exact contact email match, single client, deal found. **Medium** =
  domain-only match, contact with no company (use contact's deals), or several clients with a clear majority.
  **Low** = tie between clients, topic-only guess, no eligible deal, or no external attendee emails.
- **FR-10** Every match records its reason and how many candidate deals existed, shown in the rep summary.

### Extraction (agent, read-only step)
- **FR-11** The agent receives only the fenced transcript and context and writes `extraction.json`; it has no
  Zoom/HubSpot tool use in this step.
- **FR-12** Output: up to 5 note sentences, objections, timeline signals, next steps (us/them), contact roles,
  sentiment; every item has a verbatim quote and a confidence.

### Validation and writing (code)
- **FR-13** Drop any item whose quote is not found verbatim in the transcript; drop low-confidence items; drop
  items containing URLs or HTML. Dropped items are listed as "withheld" in the rep summary.
- **FR-14** **HubSpot note or meeting body is limited to 5 sentences**, enforced by a sentence counter that errs
  strict; excess is truncated lowest-priority-last. A one-line footer (date, call #, match confidence, medium-content
  flag, dedupe marker) is metadata and not counted. **Default object is a HubSpot meeting** (decided); `write_as: note` switches to a note. Tasks have a short title and
  one-line body and are exempt from the 5-sentence rule.
- **FR-15** On high/medium match: create the note/meeting associated to the selected deal, owned by the host's
  HubSpot owner (fallback: the deal owner). Create up to 3 tasks for **our** next steps, assigned to the same
  owner, associated to the deal.
- **FR-16** On low match: do not write the note. Create a HubSpot task `[Call Intel Review] ...` assigned to the
  host rep with the suggested deal (if any) and the proposed note. Rep completes it to approve; edits `DEAL_ID:`
  to redirect; writes `DISMISS` to drop. Expires after 14 days. Approval writes the note using the same path.
- **FR-17** Idempotency: each note/meeting contains a `pcix-ref:<zoom uuid>` marker; before writing, the deal is
  checked for the marker, so retries, restarts and lost local state never duplicate.
- **FR-18** Call number = prior pipeline-written calls on the deal + 1.
- **FR-19** Write surface is an allowlist: create note, create meeting, create task. No method exists to update
  deal stage, amount, or any property.

### Operations
- **FR-20** `doctor` verifies config, credentials and API reachability; `--dry-run` performs reads only.
- **FR-22** Retention: `transcript.txt` is deleted as soon as a call is finished (written, skipped or sent to review);
  `context.json`/`extraction.json` are deleted 7 days after finishing; `state.db` rows after 90 days (open reviews
  and pending jobs are kept). Purge runs automatically each cycle and on demand (`purge`, with `--dry-run`); it never
  deletes outside the configured work directory. Files are created owner-only (umask 077).
- **FR-23** `doctor` reports the HubSpot token's granted scopes against the required list and probes read access to
  every object type used; Zoom checks cover token, recordings listing, transcript presence and participants access.
- **FR-21** Every cycle outputs rep-ready summaries and an explicit errors list; failures never produce partial writes
  for a meeting.

## 7. Non-functional requirements
- **Security:** secrets in environment variables only; least-privilege scopes; rep allowlist limits an
  account-level Zoom credential to opted-in users; transcripts treated as untrusted (prompt-injection safe
  by construction: extraction has no write path, writes are schema-validated and allowlisted).
- **Privacy:** transcripts are stored locally only until the call is finished (deleted at commit), with a 7-day tail for extraction/context files; HubSpot receives <= 5 sentences + tasks.
  Needs a retention policy (see Open Questions) and consent practices consistent with your recording notices.
- **Reliability:** Zoom/HubSpot calls retry on 429/5xx with backoff; polling backstops webhooks; idempotent writes.
- **Cost/latency:** one LLM extraction per call; no LLM in matching or writing.
- **Maintainability:** single skill, single config, stdlib-only code, unit tests + sample transcripts as a
  regression set for prompt changes.

## 8. Design summary
`prepare` (code) -> `extract` (agent) -> `commit` (code). State: SQLite cache for queue/review/status;
HubSpot is the source of truth for "already written". See README for diagram and setup.

## 9. Success metrics (measure during a 2-week pilot with 3-5 reps)
| Metric | Target |
|---|---|
| Recorded external calls with a HubSpot update within 1 h of transcript | >= 90% |
| Wrong-deal writes (manager audit of a sample) | 0 tolerated; target < 1% |
| Notes within the 5-sentence cap | 100% (enforced) |
| Review tasks per 100 calls | < 15, falling with CRM hygiene |
| Unsupported claims found in audit | 0 |
| Rep time spent per call | < 30 s average (review tasks only) |

## 10. Rollout
1. **Dry run** (`--dry-run`) for 3 days on 1-2 reps; compare matches to what the reps would have chosen.
2. **Pilot** 3-5 reps with `write_as: note`; weekly audit of 20 notes.
3. **Expand** by adding reps to the allowlist; enable webhook if latency matters.
4. Decide note vs. meeting objects and closed-deal handling from pilot feedback.

## 11. Risks and mitigations
| Risk | Mitigation |
|---|---|
| Latest-updated deal isn't the deal discussed (e.g., an unrelated deal was just touched by a workflow) | Rule is explicit and visible in every summary; `include_closed` option; review/correction via manager audit; revisit rule if pilot error rate is high |
| Guests join without email -> unmatched | Falls to review task; consider requiring sign-in or registration for external calls |
| Scope names/availability differ in the target accounts | Scope list verified against Zoom and HubSpot docs (Appendix C); `doctor` compares the live token and probes access; one real test write with a single rep before rollout |
| Quote check passes but meaning is distorted | Sentences are short and quote-backed; weekly audit; keep extraction prompt under regression tests |
| Prompt injection via spoken text | No write tools in extraction; allowlisted writes; URL/HTML rejected; deal properties unreachable |
| Fallback HubSpot scopes (`contacts.write` + `deals.write`) are broader than needed | Prefer `meetings.write`/`tasks.write`; if fallback is used, the code write-allowlist is the only guard (documented in README and flagged by `doctor`) |
| Account-level Zoom credential is broad | Default-deny rep allowlist; least-privilege scopes; secrets in env |
| Task creation fails after note is written | Surfaced in output; tasks not retried (accepted for v1) |

## 12. Decisions on previously open questions
| # | Question | Decision |
|---|---|---|
| 1 | Note vs. HubSpot meeting object as default | **HubSpot meeting object** (`write_as: meeting`). Note remains available as a setting. |
| 2 | Should closed deals be eligible under the latest-updated rule? | **Yes.** Closed deals are eligible (`include_closed: true`). |
| 3 | Transcript/work-file retention and host-machine access | **Adopted recommendation:** delete transcript text at commit; delete context/extraction files after 7 days; prune state rows after 90 days; align Zoom's own recording auto-delete with company policy. Host: dedicated VM or service account, 2-3 named admins, disk encryption, `data/` excluded from backups/git, secrets in env or a secret manager, credential rotation on admin departure. See FR-22 and README. |
| 4 | Must customers be told AI summarizes calls? | **No customer-facing disclosure planned** (business decision; this PRD is not legal advice). Zoom's native recording notice remains on. Revisit if the customer base expands to jurisdictions with stricter rules. |
| 5 | Confirm Zoom and HubSpot scope names | **Resolved in documentation, to be confirmed live:** see Appendix C. `doctor` verifies the live token; first real write confirms write scopes. |

## Appendix A: Changes from v0 (all approved)
Single shared pipeline instead of per-rep agents; credentials model fixed; participant emails from Zoom API;
deterministic matching; latest-updated deal rule; verifiable quotes replace self-reported confidence;
review queue moved into HubSpot tasks; extraction separated from writing with allowlisted writes;
webhook support with polling fallback; HubSpot-marker idempotency; 5-sentence cap; single source of setup docs;
sample transcripts and tests.

## Appendix B: Deviations to be aware of
- v0's "call sequence gap" flag is replaced by a call counter based on calls this skill logged plus oldest-first
  processing, because there is no reliable ground truth for calls made outside the skill.
- v0's deal-property updates (non-stage) are removed; the skill only creates notes/meetings/tasks.

## Appendix C: Required permissions (verified against vendor/partner documentation, Oct 2026)

**Zoom Server-to-Server OAuth app** (granular, admin variants)
- `cloud_recording:read:list_user_recordings:admin` (list a rep's recordings)
- `cloud_recording:read:list_recording_files:admin` (get a meeting's recording files; used for webhook-queued meetings)
- `meeting:read:list_past_participants:admin` (attendee emails)
- Creating admin's role needs "Server-to-server Auth app" and "View the recording and transcript content".
- Account needs Pro or higher, cloud recording and audio transcript enabled; optional webhook event: Recording Transcript Completed.

**HubSpot Private App** (created by a super admin)
- Read: `crm.objects.contacts.read`, `crm.objects.companies.read`, `crm.objects.deals.read`, `crm.objects.owners.read`,
  `crm.objects.notes.read`, `crm.objects.meetings.read`, `crm.objects.tasks.read`
- Write (least privilege): `crm.objects.meetings.write`, `crm.objects.tasks.write` (+ `crm.objects.notes.write` if `write_as: note`)
- Fallback if the specific write scopes are unavailable: `crm.objects.contacts.write` + `crm.objects.deals.write` (broader than needed)
- Note: HubSpot's own API reference for engagements historically lists the contacts/companies/deals write scopes; newer
  per-object activity scopes (`notes`, `meetings`, `tasks`) are documented by integrations. Both families are covered above.
- Token scopes can be inspected with HubSpot's access-token-info endpoint (used by `doctor`).
