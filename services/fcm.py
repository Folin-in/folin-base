"""Direct Android FCM HTTP v1 transport. Service-account key stays on the server."""
import asyncio
import json
import re
from pathlib import Path

import httpx
from google.auth.transport.requests import Request
from google.oauth2 import service_account

from app.config import settings

SCOPE = "https://www.googleapis.com/auth/firebase.messaging"


def message_for(token: str, content: dict) -> dict:
    return {"message": {
        "token": token,
        "notification": {"title": content["title"], "body": content["body"]},
        # expo-notifications' native Android parser exposes JSON in data.body as
        # content.data, both for foreground messages and notification taps.
        "data": {"body": json.dumps(content["data"]), "channelId": content["channelId"]},
        "android": {"priority": "HIGH", "ttl": "86400s", "notification": {
            "channel_id": content["channelId"], "sound": "default",
        }},
    }}


class FCMClient:
    def __init__(self):
        self._credentials = None
        self._project = None
        self._lock = asyncio.Lock()

    async def credentials(self):
        async with self._lock:
            if self._credentials is None:
                path = settings.firebase_service_account_file
                if not path:
                    raise RuntimeError("Firebase credentials are not configured")
                if path.strip().startswith("{"):
                    info = json.loads(path)
                else:
                    info = json.loads(Path(path).read_text())
                project = info.get("project_id", "")
                if not re.fullmatch(r"[a-z][a-z0-9-]{4,61}[a-z0-9]", project):
                    raise ValueError("Invalid Firebase project ID")
                self._credentials = service_account.Credentials.from_service_account_info(info, scopes=[SCOPE])
                self._project = project
            if not self._credentials.valid:
                request = Request()
                def bounded_request(*args, **kwargs):
                    kwargs["timeout"] = 10
                    return request(*args, **kwargs)
                await asyncio.to_thread(self._credentials.refresh, bounded_request)
            return self._project, self._credentials.token

    async def send(self, token: str, content: dict) -> tuple[str | None, str | None]:
        project, access_token = await self.credentials()
        async with httpx.AsyncClient(timeout=15) as http:
            response = await http.post(f"https://fcm.googleapis.com/v1/projects/{project}/messages:send",
                                       headers={"Authorization": f"Bearer {access_token}"},
                                       json=message_for(token, content))
        result = response.json()
        if response.is_success and result.get("name"):
            return result["name"], None
        error = result.get("error") or {}
        for detail in error.get("details", []):
            if detail.get("@type") == "type.googleapis.com/google.firebase.fcm.v1.FcmError":
                return None, detail.get("errorCode") or "UNKNOWN"
        return None, error.get("status") or f"HTTP_{response.status_code}"


fcm = FCMClient()
