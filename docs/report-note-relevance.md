# Supplementary source notes

Source notes appear only as a Report notes field inside Visit details, never as
a separate card or a second rendering of the structured fields.
`prompts/note_relevance_policy.md` is the meaning-based policy shared by the
extractor and the one-time reviewer for older reports. There is no phrase blocklist.

The transcriber copies candidate notes verbatim. The existing extractor call
classifies every candidate after filling structured report details:

- Clinical or uncertain: preserve the original note.
- Administrative only: omit it.
- Fully covered elsewhere: omit only with references to populated structured
  fields. Any additional instruction, dose, limitation or warning means keep it.

The backend validates complete index coverage and field references, then selects
original strings. It never accepts model-rewritten source text. Malformed or
incomplete decisions keep the source notes and mark the review unavailable in
analysis metadata. This is a semantic model judgement, not guaranteed accuracy.
The original document remains available. Source notes and decisions are retained
in analysis metadata/trajectories for review. Existing maximum note count still
applies after selection.

The app trims/deduplicates display strings and hides the notes field for an empty
list. Visit details is hidden only when all its structured fields and notes are empty.
It does not classify notes, and opening a report does not trigger a model call.

## Older reports

Run `./venv/bin/python scripts/review_report_notes.py --email TEST_EMAIL` to
preview a single account (model calls and trajectory logs, no report changes).
Add `--apply` to review and update that account's notes. This does not re-run
biomarker analysis. Original note arrays are preserved in `report_edits`, updates
are transactionally guarded against concurrent changes, and completed reviews
for this policy version are skipped. Failures do not erase source notes.
Reopen reports online to refresh the app's offline copies after an update.

## Checks

`pytest -q tests/test_report_notes.py tests/test_transcription.py tests/test_pipeline.py`
checks validation, failure preservation, duplication references and pipeline wiring
with model stubs. These are not model-accuracy benchmarks. Inspect a real-model
preview before applying a review to existing reports.
