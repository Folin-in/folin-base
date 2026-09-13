#!/usr/bin/env python
"""One-time semantic review of a single account's older report notes.

Default is a preview (model calls/trajectory logs, no report changes). --apply
updates only notes and review metadata, preserving originals in report_edits.
Usage: ./venv/bin/python scripts/review_report_notes.py --email TEST_EMAIL [--apply]
"""
import argparse
import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.base import client
from agents.note_reviewer import NoteReviewer
from core import cache
from db import session as db
from services.report_notes import NOTE_POLICY_VERSION, detail_sections


async def main(email: str, apply: bool):
    await db.connect()
    try:
        users = await db.fetch("select user_id from users where lower(email) = $1", email.lower().strip())
        if len(users) != 1:
            raise RuntimeError("Expected exactly one matching account; no reports changed")
        user_id = users[0]["user_id"]
        rows = await db.fetch("select * from reports where user_id = $1 and deleted_at is null", user_id)
        reviewer = NoteReviewer()
        for row in rows:
            prior = (row.get("analysis_meta") or {}).get("note_review") or {}
            if prior.get("policy") == NOTE_POLICY_VERSION and prior.get("status") == "completed":
                continue
            notes = row.get("lab_notes") or []
            if not notes:
                continue
            report_id = str(row["id"])
            review = await reviewer.run(notes, row, user_id=user_id, report_id=report_id)
            metadata = {"policy": NOTE_POLICY_VERSION, "status": "completed", "source_notes": notes,
                        "decisions": review.decisions, "reviewed_at": datetime.now(timezone.utc).isoformat()}
            if apply:
                async with db.transaction() as conn:
                    current = await conn.fetchrow("select * from reports where id = $1::uuid and user_id = $2 and deleted_at is null for update", report_id, user_id)
                    if (not current or current["lab_notes"] != notes or detail_sections(dict(current)) != detail_sections(row)
                            or current.get("analysis_meta") != row.get("analysis_meta")):
                        raise RuntimeError("Report changed during review; result not applied")
                    await conn.execute("update reports set lab_notes = $1, analysis_meta = coalesce(analysis_meta, '{}'::jsonb) || $2::jsonb where id = $3::uuid and user_id = $4",
                                       review.notes, {"note_review": metadata}, report_id, user_id)
                    await conn.execute("insert into report_edits (user_id, report_id, field, old_value, new_value, edited_at) values ($1, $2::uuid, 'lab_notes', $3, $4, $5)",
                                       user_id, report_id, {"lab_notes": notes}, {"lab_notes": review.notes, "note_review": metadata}, metadata["reviewed_at"])
                cache.invalidate_reports(user_id)
            categories = [d["category"] for d in review.decisions]
            print(f"{'Updated' if apply else 'Preview'} {report_id}: {len(notes)} -> {len(review.notes)} notes; {categories}", flush=True)
    finally:
        await client.close()
        await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.email, args.apply))
