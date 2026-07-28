"""Supabase Storage: original uploaded report files in a private bucket.
All calls are best-effort from the caller's perspective - analysis must never fail
because storage is down or unconfigured.
"""
import logging

import httpx

import config

logger = logging.getLogger(__name__)

BUCKET = "reports"


def _configured() -> bool:
    return bool(config.SUPABASE_URL and config.SUPABASE_SERVICE_KEY)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.SUPABASE_SERVICE_KEY}",
        "apikey": config.SUPABASE_SERVICE_KEY,
    }


async def upload_report_file(path: str, raw: bytes, mime_type: str) -> bool:
    """Upload to <bucket>/<path>. Creates the private bucket on first use."""
    if not _configured():
        return False
    url = f"{config.SUPABASE_URL}/storage/v1/object/{BUCKET}/{path}"
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, content=raw, headers={**_headers(), "Content-Type": mime_type})
        if resp.status_code in (400, 404) and "Bucket not found" in resp.text:
            await client.post(
                f"{config.SUPABASE_URL}/storage/v1/bucket",
                json={"id": BUCKET, "name": BUCKET, "public": False, "file_size_limit": 10 * 1024 * 1024},
                headers=_headers(),
            )
            resp = await client.post(url, content=raw, headers={**_headers(), "Content-Type": mime_type})
    if resp.status_code not in (200, 201):
        logger.error(f"Supabase upload failed ({resp.status_code}): {resp.text[:200]}")
        return False
    return True


async def signed_url(path: str, expires_in: int = 3600) -> str | None:
    """Signed download URL for a stored file (private bucket)."""
    if not _configured():
        return None
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{config.SUPABASE_URL}/storage/v1/object/sign/{BUCKET}/{path}",
                json={"expiresIn": expires_in},
                headers=_headers(),
            )
        if resp.status_code != 200:
            return None
        return f"{config.SUPABASE_URL}/storage/v1{resp.json()['signedURL']}"
    except httpx.HTTPError:
        return None


async def delete_files(paths: list[str]) -> None:
    """Best-effort delete of stored files (report delete / account delete)."""
    if not _configured() or not paths:
        return
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            await client.request(
                "DELETE",
                f"{config.SUPABASE_URL}/storage/v1/object/{BUCKET}",
                json={"prefixes": paths},
                headers=_headers(),
            )
    except httpx.HTTPError as e:
        logger.error(f"Supabase delete failed: {e}")
