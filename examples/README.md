# Examples

`transcripts/*.vtt` are sample Zoom transcripts. `expected/*.json` are the extraction.json files a correct
extraction should produce for them (the format defined in `prompts/extract.md`).

- `acme_followup` - positive call, objection, timeline, tasks for us.
- `globex_stalled` - stalled / at-risk call (budget freeze, competitor).
- `initech_noisy` - choppy audio, one low-confidence item (must be withheld) and a prompt-injection attempt
  spoken on the call (must be ignored; the code has no way to change deal amount or stage anyway).

`tests/` runs every expected file through the real validator against its transcript. Use these as the
regression set whenever you edit `prompts/extract.md`: have the agent extract each transcript and diff the result.
