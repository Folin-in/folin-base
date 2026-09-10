# Disposable email domain snapshot

Source: https://github.com/disposable-email-domains/disposable-email-domains

Pinned revision: `8d5b14f53dff80842e841ee0ae14de2962cee66c` (2026-09-08).
`disposable_email_blocklist.conf` is vendored unchanged with upstream `LICENSE.txt`.

The server loads this list once. It checks exact domains and subdomains locally;
no visitor email is sent to a domain-checking service. New disposable providers can
escape detection, so keep Turnstile, durable limits, and double opt-in enabled.

Maintenance: periodically review upstream changes, replace the snapshot and license
from a pinned revision, update this revision, and run `tests/test_disposable_email.py`.
Review reported false positives before removing a domain. Restart the backend after
updating this data file. This snapshot does not update itself.
