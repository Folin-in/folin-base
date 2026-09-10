# Analysis performance

The 200-second job deadline remains authoritative. Speed tuning is centralized in
`agents/performance.py`; this is not a promise that every document completes successfully.

| Stage | Thinking effort | AI request/retry budget | Maximum single attempt |
| --- | --- | --- | --- |
| Screening | Low | 25 s | 15 s |
| Transcription | Low | 45 s | 30 s |
| Interpretation | High | 85 s | 60 s |
| Pro verification | Medium | 30 s | 20 s |

The four request budgets total 185 seconds. Profile lookup, queueing, encoding,
trajectory logging and persistence still consume the shared 200 seconds and can
leave less time. These stage caps are initial tuning values, not measured p95s.

- Maximum two sweeps over configured models, with a one-second retry delay,
  replacing four sweeps and 20/40/60-second waits. Network failures, 408, 429 and
  server failures can fail over; authorization errors stop immediately. Other
  permanent 4xx errors are not retried on the same model.
- One shared HTTP connection pool, closed on app shutdown.
- File encodings are reused only inside an upload's context and released when it
  ends, including cancellation. No shared patient-file cache across uploads.
- Screening, transcription, interpretation, audit, plausibility checks and Pro
  verification remain in order. No quality check was removed.
- If the optional transcription pass fails/returns no rows, the report warns that
  it may omit results. If Pro verification fails, values require manual checking.
  A missing/malformed verifier mismatch list is a failure, not a clean check.
- Stage duration and completion status are saved in `reports.analysis_meta`;
  trajectories record effort, failed-attempt durations and returned token usage.

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
