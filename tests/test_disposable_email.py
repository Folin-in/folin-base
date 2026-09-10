import pytest

from core.disposable_email import BLOCKED_DOMAINS, is_disposable_email


@pytest.mark.parametrize("email", [
    "test@mailinator.com", "test@MAILINATOR.COM", "test@sub.mailinator.com",
    "test@yopmail.com", "test@10minutemail.com", "test@guerrillamail.com",
])
def test_known_disposable_domains(email):
    assert is_disposable_email(email)


@pytest.mark.parametrize("email", [
    "test@gmail.com", "test@outlook.com", "test@icloud.com", "test@proton.me",
    "test@folin.in", "mailinator.com@gmail.com", "test@folin-mailinator-lookalike.example",
    "test@mailinator.com.example.com",
])
def test_legitimate_domains_and_substring_lookalikes(email):
    assert not is_disposable_email(email)


def test_snapshot_is_loaded():
    assert len(BLOCKED_DOMAINS) > 5000
