import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.models import PushTokenRequest
from db import session as db
from services.fcm import message_for
from services.push_notifications import PushNotificationService, fcm

TOKEN = "test-device-token:" + "a" * 140


def test_android_native_token_validation():
    assert PushTokenRequest(token=TOKEN, provider="fcm", platform="android").provider == "fcm"
    for args in ({"provider": "expo", "platform": "android"}, {"provider": "fcm", "platform": "ios"}):
        with pytest.raises(ValidationError):
            PushTokenRequest(token=TOKEN, **args)
    with pytest.raises(ValidationError):
        PushTokenRequest(token="ExpoPushToken[abcdefghijklmnopqrst]", provider="fcm", platform="android")


def test_native_message_preserves_notification_tap_data():
    content = {"title": "Analysis complete", "body": "Your report is ready.",
               "channelId": "analysis-results", "data": {"type": "analysis_complete", "report_id": "synthetic"}}
    message = message_for(TOKEN, content)["message"]
    assert json.loads(message["data"]["body"]) == content["data"]
    assert message["notification"]["title"] == "Analysis complete"
    assert message["android"]["notification"]["channel_id"] == "analysis-results"


@pytest.mark.parametrize("result,expected", [(("projects/test/messages/123", None), True), ((None, "UNREGISTERED"), False), ((None, "UNAVAILABLE"), False)])
def test_fcm_send_records_acceptance_and_handles_invalid_devices(monkeypatch, result, expected):
    service = PushNotificationService()
    monkeypatch.setattr(fcm, "send", AsyncMock(return_value=result))
    monkeypatch.setattr(db, "execute", AsyncMock())
    monkeypatch.setattr(service, "_deactivate", AsyncMock())
    token = {"id": "device", "token": TOKEN, "provider": "fcm"}
    assert asyncio.get_event_loop().run_until_complete(service._send(token, "delivery")) is expected
    if expected:
        assert "status = 'accepted'" in db.execute.call_args.args[0]
        service._deactivate.assert_not_called()
    elif result[1] == "UNREGISTERED":
        service._deactivate.assert_awaited_once_with("device")
    else:
        assert "delete from push_deliveries" in db.execute.call_args.args[0]


def test_missing_firebase_credentials_is_not_reported_as_sent(monkeypatch):
    service = PushNotificationService()
    monkeypatch.setattr(fcm, "send", AsyncMock(side_effect=RuntimeError("not configured")))
    monkeypatch.setattr(db, "execute", AsyncMock())
    assert not asyncio.get_event_loop().run_until_complete(service._send({"id": "device", "token": TOKEN, "provider": "fcm"}, "delivery"))
