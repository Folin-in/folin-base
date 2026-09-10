"""Local, reviewed disposable-domain snapshot; no email addresses leave the server."""
from pathlib import Path


def _normalize(domain: str) -> str:
    return domain.strip().rstrip(".").encode("idna").decode("ascii").lower()


BLOCKED_DOMAINS = frozenset(
    _normalize(line)
    for line in (Path(__file__).parent / "data/disposable_email_blocklist.conf").read_text().splitlines()
    if line.strip() and not line.startswith("#")
)


def is_disposable_email(email: str) -> bool:
    """Match whole domains and their subdomains, never arbitrary substrings."""
    domain = _normalize(email.rsplit("@", 1)[-1])
    while "." in domain:
        if domain in BLOCKED_DOMAINS:
            return True
        domain = domain.split(".", 1)[1]
    return False
