# Analysis performance

There is no overall job deadline. Per-AI-call speed tuning is centralized in
`agents/performance.py`; this is not a promise that every document completes successfully.

## Configured models (2026-09-11)

The current failover order is `gemini-3.8-flash`, `gemini-3.7-flash`, then
`gemini-3.5-flash-lite`. These explicit IDs replace the old Gemini 3 preview and
floating aliases. Google lists 3.8 Flash as the latest stable Flash and 3.5
Flash-Lite as the latest Flash-Lite; all three are present in this project's API
model catalog. See [Google's model catalog](https://ai.google.dev/gemini-api/docs/models).
All three support our low/medium/high thinking settings. They are models from the
same provider, not independent-provider redundancy. Availability and model quality
still require live checks; upgrading models does not remove the per-stage timeouts.

## Request budgets

| Stage | Thinking effort | AI request/retry budget | Maximum single attempt |
| --- | --- | --- | --- |
| Screening | Low | 25 s | 8 s |
| Transcription | Low | 36 s | 12 s |
| Interpretation | High | 120 s | 60 s ceiling; initially ~40 s with three eligible models |
| Pro verification (background) | Medium | 30 s | 10 s |

The three foreground AI request budgets total 181 seconds. Profile lookup, queueing,
encoding, trajectory logging and persistence take additional time and are not subject
to an overall 200-second cancellation. Pro verification is queued after saving.
These stage caps are tuning values, not measured p95s.

- Interpretation allows at most two sweeps; screen/transcribe/verify allow one.
  Interpretation has a one-second retry delay,
  replacing four sweeps and 20/40/60-second waits. Network failures, 408, 429 and
  server failures can fail over; authorization errors stop immediately. Other
  permanent 4xx errors are not retried on the same model.
- Each attempt receives at most its stage's per-attempt ceiling and an equal share
  of the remaining stage time among eligible models left in that sweep. Three
  interpretation timeouts therefore get roughly 40 seconds each, not 60 + 25 + 0.
  Fast failures release unused time to later models. Models that already timed out
  in this call are not retried; cooldown and authorization handling remain in place.
- One shared HTTP connection pool, closed on app shutdown.
- File encodings are reused only inside an upload's context and released when it
  ends, including cancellation. No shared patient-file cache across uploads.
- Screening, transcription, interpretation, audit and plausibility checks precede
  persistence. Pro verification runs afterward through the durable background queue.
  No quality check or interpretation thinking effort was removed by this timeout fix.
- If the optional transcription pass fails/returns no rows, the report warns that
  it may omit results. If Pro verification fails, values require manual checking.
  A missing/malformed verifier mismatch list is a failure, not a clean check.
- Stage duration and completion status are saved in `reports.analysis_meta`;
  trajectories record effort, failed-attempt durations and returned token usage.
  Failed attempts also record their allocated timeout for diagnosing fallback starvation.
  HTTP failures additionally retain sanitized Google `error.code`, `error.status`
  and `error.message` in each attempt's `provider_error`, including when a later
  fallback succeeds. Server warnings include the same diagnostics and stage/timing.
  Raw response bodies, arbitrary error details and request headers are not logged;
  known secrets/input echoes are redacted and messages are bounded. Non-JSON or
  oversized error bodies are represented by an omission marker. A timeout has no
  provider response body and is recorded as its local/transport exception class.

The September 11 fallback regression is covered offline: two full timeouts must
leave time for a third model to succeed; all-model timeouts still terminate, and
long-running jobs can still save after 200 seconds. These tests do not establish
live-provider availability or extraction accuracy on a real upload.

Thinking settings use the provider's [thinking configuration](https://ai.google.dev/gemini-api/docs/thinking).
Gemini 3 uses levels; Gemini 2.5 uses token budgets. Unknown aliases keep provider
defaults. Reduced thinking requires evaluation on representative documents before
claiming unchanged accuracy; retaining checks alone does not prove equivalence.

## Synthetic smoke benchmark

Run `./venv/bin/python -m scripts.benchmark_analysis --live` from the backend.
This is explicitly opt-in because it makes billable Gemini calls. It generates a
two-page, 12-row PNG report locally, uses the real AI stages, and bypasses all
database/storage writes. No patient documents or account quota are used.

The comparison uses previous high-effort/retry settings versus optimized settings.
Both use the new connection/cache plumbing; this does not measure those savings.
Exact-name/value/unit/range counts are checked against the synthetic source, with
only whitespace, case, micro-unit and dash normalization. Aliases may undercount.
The tool also records failed runs rather than reporting them as speedups.
One paired run is neither a latency distribution nor clinical validation.

On 2026-09-10 the optimized run took **46.146 s**, versus **65.646 s** with the
previous effort/retry settings (29.7% less time). Both returned 12/12 expected rows
with 12/12 exact values, units and reference ranges; both optional checks completed.
No retries were needed. This clean fixture does not test timeout tradeoffs, false
accepts, verifier error detection or degraded scans. Provider load/order/caching can
affect a single pair (the second run reported some cached interpretation input).
See [raw synthetic measurements](benchmarks/analysis-speed-2026-09-10.json).

Before production rollout, compare a consented or synthetic representative corpus
(long/dense scans, blurry photos, multiple pages, non-lab documents, mixed patients
and prompt injection). Track p50/p95 latency, failure rate, extracted-row recall,
field accuracy and incomplete-check rate. Do not trade missing results for speed.
