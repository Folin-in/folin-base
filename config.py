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
JWT_ALGORITHM = os.environ.get("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.environ.get("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
REFRESH_TOKEN_DAYS = int(os.environ.get("REFRESH_TOKEN_DAYS", "30").strip('"') or "30")

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
CONSENT_VERSION = os.environ.get("CONSENT_VERSION", "2026-07-29")

# Pro plan commercial terms - env-driven so price/term changes never need a deploy
PRO_TERM_DAYS = int(os.environ["PRO_TERM_DAYS"])
PRO_PRICE_INR = int(os.environ["PRO_PRICE_INR"])

# SEC-002: self-serve Pro toggle. TRUE only for preview/demo. In production this MUST be
# false so plan changes come exclusively from a verified payment webhook (Stripe).
ALLOW_SELF_SERVE_PRO = os.environ.get("ALLOW_SELF_SERVE_PRO", "true").strip().lower() == "true"

# Social login. Google: comma-separated OAuth client IDs allowed as `aud` (iOS + web).
# Empty → /auth/google returns 503. Apple: verified against the app's bundle id.
GOOGLE_CLIENT_IDS = [x.strip() for x in os.environ.get("GOOGLE_CLIENT_IDS", "").split(",") if x.strip()]
APPLE_BUNDLE_ID = os.environ.get("APPLE_BUNDLE_ID", "com.vitalquest.app")

# Resend email (for family invites)
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM = os.environ.get("RESEND_FROM", "VitalQuest <onboarding@resend.dev>")
APP_INVITE_URL = os.environ.get("APP_INVITE_URL", "https://vitalquest.app")

# SMTP for password reset (endpoints return 503 when host is unset)
SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
try:
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587") or "587")
except ValueError:
    SMTP_PORT = 587
SMTP_USER = os.environ.get("SMTP_USER", "").strip()
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "").strip() or SMTP_USER

SMTP_ENABLED = bool(SMTP_HOST and SMTP_USER and SMTP_PASS and SMTP_FROM)

# SEC/HARDENING-2: CORS allow-list.
_raw = os.environ.get("CORS_ALLOWED_ORIGINS", "*").strip()
if _raw == "*":
    CORS_ALLOWED_ORIGINS = ["*"]
else:
    CORS_ALLOWED_ORIGINS = [o.strip() for o in _raw.split(",") if o.strip()]

# HARDENING-1: trusted proxies.
TRUSTED_PROXY_IPS = {
    ip.strip() for ip in os.environ.get(
        "TRUSTED_PROXY_IPS",
        "127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16",
    ).split(",")
    if ip.strip()
}
