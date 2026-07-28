"""One-time data copy: Mongo (vitalquest) -> Supabase Postgres.
Run once after SUPABASE_DB_URL is set, then Mongo can be shut down:
    ./venv/bin/python scripts/migrate_mongo_to_pg.py
Idempotent: rows are upserted/skipped on conflict.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import database  # noqa: E402
from pymongo import MongoClient  # noqa: E402

REPORT_COLS = [
    "user_id", "filename", "mime_type", "storage_path", "uploaded_at", "report_type", "title",
    "report_date", "patient_name", "lab_name", "summary", "overall_assessment", "positives",
    "concerns", "suggestions", "biomarkers", "health_score", "normal_count", "attention_count",
    "total_count", "chief_complaint", "exam_findings", "diagnoses", "referrals", "medications",
    "modality", "body_area", "findings", "impression", "admission_date", "discharge_date",
    "procedures", "follow_up",
]
JSON_COLS = {"positives", "concerns", "suggestions", "biomarkers", "exam_findings", "diagnoses",
             "referrals", "medications", "findings", "procedures"}
INT_COLS = {"health_score", "normal_count", "attention_count", "total_count"}


async def main():
    mongo = MongoClient("mongodb://localhost:27017")["vitalquest"]
    await database.connect()

    users = list(mongo.users.find({}, {"_id": 0}))
    for u in users:
        await database.execute(
            """insert into users (user_id, email, username, name, password_hash, auth_provider,
                                  picture, health_profile, preferences, token_version, created_at)
               values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
               on conflict (user_id) do nothing""",
            u["user_id"], u["email"], u.get("username"), u.get("name") or "", u.get("password_hash"),
            u.get("auth_provider") or "local", u.get("picture"), u.get("health_profile") or {},
            u.get("preferences") or {}, int(u.get("token_version") or 0), u.get("created_at") or "",
        )
    print(f"users: {len(users)}")

    # Reports get fresh uuids, so guard against double-copy instead of upserting
    if await database.fetchval("select count(*) from reports") > 0:
        print("reports: table not empty, skipping")
        reports = []
    else:
        reports = list(mongo.reports.find({}, {"_id": 0}))
    for r in reports:
        vals = []
        for c in REPORT_COLS:
            v = r.get(c)
            if c in JSON_COLS:
                v = v or []
            elif c in INT_COLS:
                v = int(v or 0)
            elif c in ("filename", "mime_type", "uploaded_at", "summary", "overall_assessment"):
                v = v or ""
            elif c == "report_type":
                v = v or "lab"
            vals.append(v)
        placeholders = ", ".join(f"${i + 1}" for i in range(len(REPORT_COLS)))
        await database.execute(
            f"insert into reports ({', '.join(REPORT_COLS)}) values ({placeholders})", *vals
        )
    print(f"reports: {len(reports)}")

    invites = list(mongo.family_invites.find({}))
    for i in invites:
        await database.execute(
            """insert into family_invites (id, inviter_id, inviter_name, invitee_email, invitee_user_id,
                                           relationship, status, created_at, accepted_at, declined_at, resend_email_id)
               values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
               on conflict (id) do nothing""",
            str(i["_id"]), i["inviter_id"], i.get("inviter_name") or "", i["invitee_email"],
            i.get("invitee_user_id"), i.get("relationship") or "family", i.get("status") or "pending",
            str(i.get("created_at") or ""), str(i["accepted_at"]) if i.get("accepted_at") else None,
            str(i["declined_at"]) if i.get("declined_at") else None, i.get("resend_email_id"),
        )
    print(f"family_invites: {len(invites)}")

    links = list(mongo.family_links.find({}, {"_id": 0}))
    for l in links:
        await database.execute(
            """insert into family_links (a, b, relationship, created_at) values ($1, $2, $3, $4)
               on conflict (a, b) do nothing""",
            l["a"], l["b"], l.get("relationship") or "family", str(l.get("created_at") or ""),
        )
    print(f"family_links: {len(links)}")

    await database.close()
    print("done")


if __name__ == "__main__":
    asyncio.run(main())
