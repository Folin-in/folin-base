---
name: analysis_pipeline
kind: orchestrator
description: Owns the upload->report flow. Calls the screener, transcriber, extractor, deterministic auditor, then (Pro) the verifier. A rejection at any gate stops the run.
model_call: none
includes: []
tools: [sanity_bounds, name_match]
subagents: [screener, transcriber, extractor, auditor, verifier]
output: report
trajectory_kind: none
---

# Analysis pipeline

Order is fixed; screening and auditing are mandatory gates:

1. **screener** - a separate model call with no extraction task. Rejects prompt
   injection, disguised documents and non-medical files. Fails closed.
2. **transcriber** - enumerates printed result rows before interpretation. Its rows
   are passed to the extractor and used to restore omitted markers deterministically.
3. **extractor** - produces the report JSON. With several photos it must first
   confirm they are pages of ONE report (`pages_coherent`); if not, the run stops
   and the user is told which photo did not belong.
4. **auditor** - deterministic, no model. Walks every string in the JSON for obeyed
   instructions and wellness-boundary breaches. Not itself injectable.
5. **sanity_bounds** - drops biomarker values outside physiological plausibility.
6. **verifier** (Pro) - second model pass cross-checking values against the document.

A rejection at 1, 3 or 4 is a user-file problem: HTTP 422, quota refunded. A required-model
outage is a 502 and is also refunded. Transcription/verification failures are non-fatal,
but incomplete checks are recorded and shown as warnings. "Not a medical document" is a 400 and
still counts - the work was done.

Stage-specific effort and retry budgets are defined in `agents/performance.py`.
Every stage remains within the job's shared 200-second deadline; no checks run
in parallel with their dependencies or after that deadline.
