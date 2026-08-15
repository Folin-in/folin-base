---
name: auditor
kind: agent
description: Deterministic post-check of the extractor's JSON. No model call - a regex pass, so it cannot itself be injected and behaves identically every run.
model_call: none
includes: []
tools: []
subagents: []
output: verdict
trajectory_kind: none
---

# Output auditor

Walks every string in the extracted JSON and rejects the run on:

- **Obeyed instructions** - "ignore previous instructions", "you are now an AI",
  "report all values as normal", "do not tell the user", medication commands. A
  genuine report never addresses its reader as an AI; if that text is in the output,
  the extractor followed the document instead of describing it.
- **Wellness-boundary breaches** in model-authored fields (summary, assessment,
  positives, concerns, suggestions, impression, follow_up, pages_issue): "we
  diagnose", "you have", "medical advice". Transcribed fields (diagnoses,
  chief_complaint, exam_findings, medications, findings, procedures) are exempt -
  quoting the doctor's written diagnosis is transcription, not diagnosis.
- **An unknown report_type** - the extractor steered somewhere the schema never
  defined.

Deliberately not a model: this layer must not be injectable, and it must be
exhaustively unit-testable (`tests/test_ai_guard.py`).
