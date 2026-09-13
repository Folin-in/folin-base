# Analysis lifecycle (no overall deadline)

The former 200-second upload cutoff has been removed. Queue time, profile lookup,
AI processing and persistence no longer share an elapsed-time cancellation timer.
An otherwise valid report is not discarded just because saving passes 200 seconds.

Individual AI calls still have bounded stage budgets, per-attempt timeouts and
finite retries; see [performance tuning](analysis-performance.md). Actual provider,
validation and storage/database errors can still fail an analysis.

The API returns `null` for the legacy `timeout_seconds` and `remaining_seconds`
fields. The app polls until success/error or until the screen is left. It keeps
the pending-job reference for background tracking and reconnects after network
errors. Old locally stored deadline fields are ignored. Existing failed jobs are
not automatically retried; their temporary upload payloads were already erased.

## Worker recovery

A worker renews `heartbeat_at` every 15 seconds. Another worker can reclaim work
after 180 seconds without renewal; this is a renewable ownership lease, not a
limit on how long a live analysis can take. Pending work does not expire by age.
The `started_at` value identifies the current claim. Renewal, shutdown handoff,
failure and report persistence check that claim so a stale worker cannot overwrite
a reclaimed job. Heartbeats skip rows locked by report saving rather than waiting
on their own transaction. Lost renewal/ownership cancels the old worker's work.

Report insertion and successful job completion remain one transaction, together
with completion-notification and optional verification enqueueing. Temporary upload
payloads are erased on success/failure; interrupted work retains them for recovery.

Deploy schema `024_analysis_heartbeat.sql` before starting updated workers. Stop
old worker processes during deployment: their age-based expiry sweeper must not
run alongside the new worker. Update the mobile client too; older clients contain
the former local 200-second timer.
