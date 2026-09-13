import json

import httpx
import pytest

from agents.provider_errors import MAX_ERROR_BODY, MAX_MESSAGE, provider_error_details


def response(error):
    return httpx.Response(503, json={"error": error})


def test_retains_google_message_status_and_code_not_arbitrary_payload():
    result = provider_error_details(response({
        "code": 503, "status": "UNAVAILABLE",
        "message": "The model is overloaded. Please try again later.",
        "details": [{"patient": "must not be retained"}],
    }))
    assert result == {"code": 503, "status": "UNAVAILABLE",
                      "message": "The model is overloaded. Please try again later."}


def test_redacts_credentials_payload_echoes_and_log_injection():
    secret = "AIza-test-private-key"
    prompt = "Patient: Example Person\nReport content: sensitive results"
    result = provider_error_details(response({
        "status": "UNAVAILABLE",
        "message": f'Failed {secret}; {prompt}; {json.dumps(prompt)}; "private excerpt"; '
                   "'single quoted excerpt'; contact patient@example.com; "
                   "https://example.com/?key=private; Bearer another-secret\nFORGED\tLOG " + "a" * 200,
    }), [secret, prompt])
    text = json.dumps(result)
    for private in (secret, "Example Person", "sensitive results", "private excerpt", "single quoted excerpt",
                    "patient@example.com", "another-secret", "https://example.com", "a" * 100):
        assert private not in text
    assert "\n" not in result["message"] and "\t" not in result["message"]


@pytest.mark.parametrize("body", [b'<html>secret content</html>', b'null', b'[]', b'{"error":"secret"}', b'{}'])
def test_non_google_error_bodies_are_not_dumped(body):
    result = provider_error_details(httpx.Response(503, content=body))
    assert "secret" not in json.dumps(result)
    assert "unavailable" in result["message"]


def test_oversized_and_long_messages_are_bounded():
    assert "too large" in provider_error_details(httpx.Response(503, content=b'x' * (MAX_ERROR_BODY + 1)))["message"]
    result = provider_error_details(response({"message": "Try again later. " * 200}))
    assert len(result["message"]) == MAX_MESSAGE + 1


def test_unexpected_types_and_untrusted_status_are_omitted():
    assert provider_error_details(response({"code": {}, "status": "private patient data", "message": []})) == {
        "message": "[provider error fields unavailable]"}
