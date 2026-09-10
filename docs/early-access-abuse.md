# Web early-access abuse protection

Applies only to `POST /api/early-access`, not mobile authentication.

- Honeypot submissions return the uniform response without database/provider work.
- Valid form submissions consume shared PostgreSQL counters before external calls:
  3 per IP per UTC minute and 5 per IP per UTC hour by default. These are fixed
  windows, not rolling windows; budgets reset at the clock boundary.
- Known disposable domains and their subdomains are rejected before Turnstile or
  Resend calls. Rejections still consume the IP budget.
- Turnstile must pass before claiming a confirmation email.
- One confirmation per normalized email per 24 hours; confirmed or suppressed
  addresses get the same response without another send.
- The global early-access cap is 80 confirmation sends per UTC day. It does not
  include other product emails. Claims are transactional; failed sends release
  the claim and daily quota, but do not refund IP attempts.
- Rate-limit responses use HTTP 429 with `Retry-After` seconds. The form retains
  server errors when a refreshed Turnstile challenge completes.

IP keys use HMACs, not raw addresses. IPv6 clients share a /64 budget (IPv4-mapped
IPv6 uses the corresponding IPv4 key). Forwarded headers are accepted only from
`TRUSTED_PROXY_IPS`; configure only the actual proxy addresses/CIDRs, never all IPs.
Shared Wi-Fi users share a budget, so tune limits if legitimate visitors are blocked.

Settings: `EARLY_ACCESS_IP_MINUTE_LIMIT`, `EARLY_ACCESS_IP_HOURLY_LIMIT`,
`EARLY_ACCESS_DAILY_LIMIT`, `EARLY_ACCESS_CONFIRMATION_COOLDOWN_MINUTES`.
No new schema is needed: existing `early_access_limits` stores both IP windows
and global counters. Expired rows may be pruned by scheduled database maintenance
using `delete from early_access_limits where expires_at < now()`.

Before production: use real Turnstile keys and expected hostname/action, a verified
Resend sender, and a Resend webhook secret for bounce/complaint suppression.
Distributed attackers and new disposable domains are not fully prevented by these
controls; keep the daily cap and monitor rejections/provider delivery failures.
