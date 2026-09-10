# Analysis deadline

Accepted analysis jobs have one 200-second processing budget. It starts when the
enqueue handler receives the parsed upload; transmitting the file to the API and
client-side file encoding are outside this budget.

The original `analyze_jobs.created_at` determines the deadline. Queue time, profile
lookup, all AI stages, retries, backoff, file storage and report persistence consume
the same budget. Restarting or reclaiming a job never resets it.

The runner cancels outstanding asynchronous work at expiry. An independent sweep
marks expired queued/orphaned jobs as errors, normally within its two-second polling
interval. Status queries and new-upload checks also expire overdue jobs. Terminal
state persistence and network delivery may take additional time; this is not a
hard real-time response guarantee during database or network outages. Cancellation
closes Folin's request but cannot guarantee that an external AI provider stops its
own processing or billing.

Report insertion and successful job completion share a database transaction and
lock the job row. Completion checks the deadline again before marking success, so
an expired save rolls back the report. Failure updates cannot overwrite success.
Timed-out jobs clear their stored input payload and do not count toward the free
trial's successful-analysis allowance.

The enqueue/status API exposes `timeout_seconds` and `remaining_seconds`. The app
uses the remaining budget and a separate timer to stop its spinner even if polling
hangs. If the final response is lost, it retains the job reference so a result that
finished just before the deadline can still be recovered.

The deadline itself enforces an upper processing budget, not successful completion.
The separate [performance tuning](analysis-performance.md) changes effort and retry
budgets while retaining the pipeline's checks.
