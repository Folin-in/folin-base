---
name: analysis_pipeline
kind: orchestrator
description: Owns the upload->report flow. Calls the screener, then the extractor, then the deterministic auditor, then (Pro) the verifier. A rejection at any gate stops the run.
model_call: none
includes: []
tools: [sanity_bounds, name_match]
subagents: [screener, transcriber, extractor, auditor, verifier]
output: report
trajectory_kind: none
---

# Analysis pipeline

Order is fixed and every step is a gate:

1. **screener** - a separate model call with no extraction task. Rejects prompt
   injection, disguised documents and non-medical files. Fails closed.
2. **extractor** - produces the report JSON. With several photos it must first
   confirm they are pages of ONE report (`pages_coherent`); if not, the run stops
   and the user is told which photo did not belong.
3. **auditor** - deterministic, no model. Walks every string in the JSON for obeyed
   instructions and wellness-boundary breaches. Not itself injectable.
4. **sanity_bounds** - drops biomarker values outside physiological plausibility.
5. **verifier** (Pro) - second model pass cross-checking values against the document.

A rejection at 1, 2 or 3 is a user-file problem: HTTP 422, quota refunded. A model
outage anywhere is a 502 and is also refunded. "Not a medical document" is a 400 and
still counts - the work was done.
