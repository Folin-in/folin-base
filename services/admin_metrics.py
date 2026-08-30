"""Read-only rollups behind the admin dashboard.

Every timestamp column in this schema is ISO-8601 UTC text, which sorts lexicographically,
so a window is just `column >= cutoff_string` - no casting on the filter and the existing
indexes still apply. Durations and percentiles do cast, but only over rows already filtered.

Nothing here writes, and nothing takes a caller-supplied identifier: the dashboard is a
whole-tenant view, so the SQL is fixed and the only parameter is the window cutoff.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.constants import METRIC_RECENT_JOBS, METRIC_SLOW_CALL_MS, METRIC_TOP_ERRORS, METRIC_WINDOWS
from app.enums import JobStatus, PaymentStatus
from db import session as db

_PAID = str(PaymentStatus.PAID)
_SUCCESS = str(JobStatus.SUCCESS)


def cutoff(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _pct(part: int, whole: int) -> float:
    return round(part * 100 / whole, 1) if whole else 0.0


def _change(now: int, prev: int) -> float | None:
    """Percent change, or None when there is no baseline - a jump from zero is not
    an infinite improvement and must not be drawn as one."""
    return round((now - prev) * 100 / prev, 1) if prev else None


@dataclass(frozen=True)
class WindowCounts:
    signups: int
    uploads: int
    jobs_ok: int
    reports: int
    revenue_inr: int

    @property
    def success_rate(self) -> float:
        return _pct(self.jobs_ok, self.uploads)

    def as_dict(self) -> dict:
        return {"signups": self.signups, "uploads": self.uploads, "jobs_ok": self.jobs_ok,
                "reports": self.reports, "revenue_inr": self.revenue_inr,
                "success_rate": self.success_rate}


class AdminMetricsService:
    """One call per panel. Each returns plain dicts ready to serialise."""

    async def overview(self) -> dict:
        out = {}
        for label, days in METRIC_WINDOWS.items():
            since = cutoff(days)
            row = await db.fetchrow(
                """select
                     (select count(*) from users where created_at >= $1)                      as signups,
                     (select count(*) from analyze_jobs where created_at >= $1)               as uploads,
                     (select count(*) from analyze_jobs where created_at >= $1 and status = $2) as jobs_ok,
                     (select count(*) from reports where uploaded_at >= $1)                   as reports,
                     (select coalesce(sum(amount_inr), 0) from payments
                        where created_at >= $1 and status = $3)                               as revenue_inr""",
                since, _SUCCESS, _PAID)
            out[label] = WindowCounts(**{k: int(v or 0) for k, v in row.items()}).as_dict()
        return out

    async def totals(self) -> dict:
        row = await db.fetchrow(
            """select
                 (select count(*) from users)                                     as users,
                 (select count(*) from reports)                                   as reports,
                 (select count(*) from analyze_jobs)                              as jobs,
                 (select count(*) from members)                                   as members,
                 (select coalesce(sum(amount_inr), 0) from payments where status = $1) as revenue_inr,
                 (select coalesce(round(avg(health_score)), 0) from reports
                    where health_score > 0)                                       as avg_score""",
            _PAID)
        return {k: int(v or 0) for k, v in row.items()}

    async def jobs(self, days: int) -> dict:
        since = cutoff(days)
        status = await db.fetch(
            "select status, count(*) as n from analyze_jobs where created_at >= $1 group by status order by n desc",
            since)
        errors = await db.fetch(
            """select error, count(*) as n from analyze_jobs
               where created_at >= $1 and error is not null group by error order by n desc limit $2""",
            since, METRIC_TOP_ERRORS)
        timing = await db.fetchrow(
            """select
                 count(*) as n,
                 coalesce(round(percentile_cont(0.5) within group (
                   order by extract(epoch from completed_at::timestamptz - started_at::timestamptz))), 0) as p50_s,
                 coalesce(round(percentile_cont(0.95) within group (
                   order by extract(epoch from completed_at::timestamptz - started_at::timestamptz))), 0) as p95_s
               from analyze_jobs
               where created_at >= $1 and status = $2 and started_at is not null and completed_at is not null""",
            since, _SUCCESS)
        recent = await db.fetch(
            """select job_id, status, error, created_at, completed_at
               from analyze_jobs order by created_at desc limit $1""",
            METRIC_RECENT_JOBS)
        return {"by_status": [dict(r) for r in status],
                "top_errors": [dict(r) for r in errors],
                "duration": {k: int(v or 0) for k, v in timing.items()},
                "recent": [dict(r) for r in recent]}

    async def models(self, days: int) -> dict:
        since = cutoff(days)
        per_model = await db.fetch(
            """select coalesce(model, 'none') as model,
                      count(*) as calls,
                      count(*) filter (where status = 'ok')         as ok,
                      count(*) filter (where status = 'bad_reply')  as bad_reply,
                      count(*) filter (where status = 'error')      as errors,
                      coalesce(round(percentile_cont(0.5) within group (order by latency_ms)), 0) as p50_ms,
                      coalesce(round(percentile_cont(0.95) within group (order by latency_ms)), 0) as p95_ms,
                      count(*) filter (where latency_ms >= $2)      as slow
               from ai_trajectories where created_at >= $1
               group by model order by calls desc""",
            since, METRIC_SLOW_CALL_MS)
        per_kind = await db.fetch(
            """select kind, count(*) as calls,
                      coalesce(round(percentile_cont(0.5) within group (order by latency_ms)), 0) as p50_ms
               from ai_trajectories where created_at >= $1 group by kind order by calls desc""",
            since)
        rows = [dict(r) for r in per_model]
        for r in rows:
            r["ok_rate"] = _pct(r["ok"], r["calls"])
        return {"by_model": rows, "by_kind": [dict(r) for r in per_kind]}

    async def users(self) -> dict:
        providers = await db.fetch(
            "select auth_provider, count(*) as n from users group by auth_provider order by n desc")
        plans = await db.fetch(
            """select coalesce(preferences->>'plan', 'free') as plan, count(*) as n
               from users group by plan order by n desc""")
        return {"by_provider": [dict(r) for r in providers], "by_plan": [dict(r) for r in plans]}

    async def reports(self) -> dict:
        types = await db.fetch(
            "select report_type, count(*) as n from reports group by report_type order by n desc")
        flags = await db.fetchrow(
            """select count(*) filter (where name_mismatch)              as name_mismatch,
                      count(*) filter (where jsonb_array_length(coalesce(biomarkers, '[]'::jsonb)) = 0) as no_markers
               from reports""")
        return {"by_type": [dict(r) for r in types], "flags": {k: int(v or 0) for k, v in flags.items()}}

    async def payments(self) -> dict:
        row = await db.fetchrow(
            """select count(*) as orders,
                      count(*) filter (where status = $1) as paid,
                      coalesce(sum(amount_inr) filter (where status = $1), 0) as revenue_inr
               from payments""",
            _PAID)
        d = {k: int(v or 0) for k, v in row.items()}
        d["conversion"] = _pct(d["paid"], d["orders"])
        return d

    async def timeseries(self, days: int) -> list[dict]:
        """One row per day in the window, zero-filled so gaps read as gaps, not as
        missing bars. generate_series builds the spine; the counts left-join onto it."""
        rows = await db.fetch(
            """with spine as (
                 select generate_series(
                   (now() at time zone 'utc')::date - ($1::int - 1),
                   (now() at time zone 'utc')::date, '1 day')::date as day)
               select to_char(spine.day, 'YYYY-MM-DD') as day,
                      coalesce(j.total, 0)   as uploads,
                      coalesce(j.ok, 0)      as ok,
                      coalesce(j.failed, 0)  as failed,
                      coalesce(u.n, 0)       as signups
               from spine
               left join (
                 select created_at::date as day, count(*) as total,
                        count(*) filter (where status = $2) as ok,
                        count(*) filter (where status <> $2) as failed
                 from analyze_jobs group by 1) j on j.day = spine.day
               left join (
                 select created_at::date as day, count(*) as n from users group by 1) u
                 on u.day = spine.day
               order by spine.day""",
            days, _SUCCESS)
        return [dict(r) for r in rows]

    async def deltas(self, days: int) -> dict:
        """This window against the one immediately before it, so a number can say
        which way it is moving instead of just how big it is."""
        now, prev = cutoff(days), cutoff(days * 2)
        row = await db.fetchrow(
            """select
                 (select count(*) from analyze_jobs where created_at >= $1)                     as uploads,
                 (select count(*) from analyze_jobs where created_at >= $2 and created_at < $1) as uploads_prev,
                 (select count(*) from users where created_at >= $1)                            as signups,
                 (select count(*) from users where created_at >= $2 and created_at < $1)        as signups_prev,
                 (select coalesce(sum(amount_inr), 0) from payments
                    where created_at >= $1 and status = $3)                                     as revenue,
                 (select coalesce(sum(amount_inr), 0) from payments
                    where created_at >= $2 and created_at < $1 and status = $3)                 as revenue_prev""",
            now, prev, _PAID)
        d = {k: int(v or 0) for k, v in row.items()}
        return {m: {"now": d[m], "prev": d[f"{m}_prev"],
                    "change": _change(d[m], d[f"{m}_prev"])}
                for m in ("uploads", "signups", "revenue")}

    async def snapshot(self, days: int) -> dict:
        """Everything the dashboard renders, in one round of queries."""
        return {"generated_at": datetime.now(timezone.utc).isoformat(),
                "window_days": days,
                "windows": list(METRIC_WINDOWS),
                "overview": await self.overview(),
                "timeseries": await self.timeseries(days),
                "deltas": await self.deltas(days),
                "totals": await self.totals(),
                "jobs": await self.jobs(days),
                "models": await self.models(days),
                "users": await self.users(),
                "reports": await self.reports(),
                "payments": await self.payments()}


admin_metrics = AdminMetricsService()
