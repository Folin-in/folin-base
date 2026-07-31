import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

JWT_SECRET = os.environ["JWT_SECRET"]
# SEC-001: refuse weak / default signing keys at startup so a misconfigured deploy
# can never silently run with a guessable secret.
_WEAK_MARKERS = ("change-in-prod", "changeme", "secret", "default", "example")
if len(JWT_SECRET) < 32 or any(m in JWT_SECRET.lower() for m in _WEAK_MARKERS):
    raise RuntimeError(
        "JWT_SECRET is too weak. Set a random value of at least 32 chars, e.g. "
        "python -c \"import secrets; print(secrets.token_urlsafe(48))\""
    )
JWT_ALGORITHM = os.environ["JWT_ALGORITHM"]
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.environ["ACCESS_TOKEN_EXPIRE_MINUTES"])
REFRESH_TOKEN_DAYS = int(os.environ["REFRESH_TOKEN_DAYS"])

# Gemini (medical document analysis). GEMINI_MODELS is a comma-separated priority
# list - the agent tries each in order and fails over automatically on any error.
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODELS = [m.strip() for m in os.environ["GEMINI_MODELS"].split(",") if m.strip()]
if not GEMINI_MODELS:
    raise RuntimeError("GEMINI_MODELS must list at least one model")

# Supabase: Storage (original uploaded report files) + Postgres (all app data)
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
SUPABASE_DB_URL = os.environ["SUPABASE_DB_URL"]

# Analysis quotas: free accounts get a TOTAL trial allowance (not per day);
# Pro gets a daily cap for its 90-day term.
FREE_TOTAL_ANALYSES = int(os.environ["FREE_TOTAL_ANALYSES"])
PRO_DAILY_ANALYSES = int(os.environ["PRO_DAILY_ANALYSES"])

# Health-data consent: bump when the privacy policy materially changes so users re-consent.
CONSENT_VERSION = os.environ["CONSENT_VERSION"]

# Pro plan commercial terms - env-driven so price/term changes never need a deploy
PRO_TERM_DAYS = int(os.environ["PRO_TERM_DAYS"])
PRO_PRICE_INR = int(os.environ["PRO_PRICE_INR"])

# SEC-002: self-serve Pro toggle. TRUE only for preview/demo. In production this MUST be
# false so plan changes come exclusively from a verified payment webhook (Stripe).
ALLOW_SELF_SERVE_PRO = os.environ["ALLOW_SELF_SERVE_PRO"].strip().lower() == "true"

# Social login. Google: comma-separated OAuth client IDs allowed as `aud` (iOS + web).
# Empty → /auth/google returns 503. Apple: verified against the app's bundle id.
GOOGLE_CLIENT_IDS = [x.strip() for x in os.environ.get("GOOGLE_CLIENT_IDS", "").split(",") if x.strip()]
APPLE_BUNDLE_ID = os.environ["APPLE_BUNDLE_ID"]

# Resend email (for family invites). Empty API key = invites sent without email.
# When the key IS set, the sender identity must be explicit - no baked-in default.
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM = os.environ["RESEND_FROM"] if RESEND_API_KEY else ""
APP_INVITE_URL = os.environ["APP_INVITE_URL"]

# SMTP for password reset (endpoints return 503 when host is unset). When a host
# IS configured, port/user/pass/from are all required - no silent defaults.
SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
if SMTP_HOST:
    SMTP_PORT = int(os.environ["SMTP_PORT"])
    SMTP_USER = os.environ["SMTP_USER"].strip()
    SMTP_PASS = os.environ["SMTP_PASS"]
    SMTP_FROM = os.environ["SMTP_FROM"].strip()
else:
    SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM = 0, "", "", ""

SMTP_ENABLED = bool(SMTP_HOST and SMTP_USER and SMTP_PASS and SMTP_FROM)

# SEC/HARDENING-2: CORS allow-list.
_raw = os.environ["CORS_ALLOWED_ORIGINS"].strip()
if _raw == "*":
    CORS_ALLOWED_ORIGINS = ["*"]
else:
    CORS_ALLOWED_ORIGINS = [o.strip() for o in _raw.split(",") if o.strip()]

# HARDENING-1: trusted proxies.
TRUSTED_PROXY_IPS = {ip.strip() for ip in os.environ["TRUSTED_PROXY_IPS"].split(",") if ip.strip()}
