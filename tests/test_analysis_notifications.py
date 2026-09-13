"""Completion notification tests: no network, real accounts, or pushes."""
import asyncio
from unittest.mock import AsyncMock

from services.push_notifications import ANALYSIS_KEY, PushNotificationService
from db import session as db


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def setup(monkeypatch, claimed=True, sent=True):
    service = PushNotificationService()
    token = {"id": "device", "user_id": "synthetic", "token": "test-token",
             "report_id": "12345678-1234-1234-1234-123456789abc"}
    monkeypatch.setattr(db, "execute", AsyncMock())
    monkeypatch.setattr(db, "fetch", AsyncMock(return_value=[token]))
    monkeypatch.setattr(db, "fetchrow", AsyncMock(return_value={"id": "delivery"} if claimed else None))
    monkeypatch.setattr(service, "_send", AsyncMock(return_value=sent))
    return service, token


def test_completion_message_is_private_and_routes_to_exact_report(monkeypatch):
    service, token = setup(monkeypatch)
    assert run(service.send_completed_analyses()) == 1
    content = service._send.call_args.args[2]
    assert content == {
        "title": "Analysis complete", "body": "Your report is ready. Tap to view it in Folin.",
        "channelId": "analysis-results",
        "data": {"type": ANALYSIS_KEY, "report_id": token["report_id"]},
    }
    query = db.fetch.call_args.args[0]
    assert "p.active = true" in query and "r.deleted_at is null" in query
    assert "('daily', 'weekly')" in query
    assert "plan" not in query  # Free users receive completion alerts too.
    assert "p.created_at <= n.created_at" in query


def test_duplicate_claim_does_not_send_again(monkeypatch):
    service, _ = setup(monkeypatch, claimed=False)
    assert run(service.send_completed_analyses()) == 0
    service._send.assert_not_called()


def test_transient_failure_backs_off(monkeypatch):
    service, _ = setup(monkeypatch, sent=False)
    assert run(service.send_completed_analyses()) == 0
    assert "interval '5 minutes'" in db.execute.call_args.args[0]


def test_no_eligible_devices_does_not_send(monkeypatch):
    service, _ = setup(monkeypatch)
    db.fetch.return_value = []
    assert run(service.send_completed_analyses()) == 0
    service._send.assert_not_called()
