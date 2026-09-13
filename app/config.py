"""Runtime configuration. Values come from the environment and are validated once at
import so a misconfigured deploy fails at boot instead of mid-request. Optional values
are called out explicitly in `.env.example`.

`settings` is the single instance the rest of the code imports.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

_WEAK_SECRET_MARKERS = ("change-in-prod", "changeme", "secret", "default", "example")
_MIN_SECRET_CHARS = 32


class ConfigError(RuntimeError):
    pass


def _req(name: str) -> str:
    try:
        return os.environ[name]
    except KeyError:
        raise ConfigError(f"Required setting {name} is not set - see .env.example.") from None


def _int(name: str) -> int:
    raw = _req(name)
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None


def _int_default(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None


def _bool(name: str) -> bool:
    return _req(name).strip().lower() == "true"


def _csv(name: str, required: bool = True) -> list[str]:
    raw = _req(name) if required else os.environ.get(name, "")
    return [x.strip() for x in raw.split(",") if x.strip()]


@dataclass(frozen=True)
class Settings:
    # --- auth
    jwt_secret: str
    jwt_algorithm: str
    access_token_expire_minutes: int
    refresh_token_days: int
    google_client_ids: list[str]
    apple_bundle_id: str
    supabase_url: str
    # --- data
    supabase_db_url: str
    supabase_service_key: str
    # --- ai
    gemini_api_key: str
    gemini_models: list[str]
    # --- product
    consent_version: str
    free_total_analyses: int
    pro_term_days: int
    pro_price_inr: int
    allow_self_serve_pro: bool
    # --- payments
    google_play_package_name: str
    google_play_pro_product_id: str
    google_play_pro_base_plan_id: str
    google_play_service_account_json: str
    google_play_rtdn_audience: str
    google_play_pubsub_service_account_email: str
    # --- email
    resend_api_key: str
    resend_from: str
    app_invite_url: str
    early_access_from: str
    early_access_confirm_url: str
    early_access_web_url: str
    early_access_daily_limit: int
    early_access_ip_hourly_limit: int
    early_access_ip_minute_limit: int
    early_access_confirmation_cooldown_minutes: int
    early_access_confirmation_expire_hours: int
    turnstile_secret_key: str
    turnstile_expected_hostname: str
    turnstile_expected_action: str
    resend_webhook_secret: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_pass: str
    smtp_from: str
    expo_access_token: str
    firebase_service_account_file: str
    # --- network
    cors_allowed_origins: list[str]
    admin_emails: list[str]
    admin_allowed_ips: list[str] = field(default_factory=list)
    trusted_proxy_ips: frozenset[str] = field(default_factory=frozenset)

    @property
    def smtp_enabled(self) -> bool:
        return bool(self.smtp_host and self.smtp_user and self.smtp_pass and self.smtp_from)

    @property
    def google_login_enabled(self) -> bool:
        return bool(self.google_client_ids)

    @property
    def supabase_auth_enabled(self) -> bool:
        return bool(self.supabase_url)


def _load() -> Settings:
    jwt_secret = _req("JWT_SECRET")
    if len(jwt_secret) < _MIN_SECRET_CHARS or any(m in jwt_secret.lower() for m in _WEAK_SECRET_MARKERS):
        raise ConfigError(
            f"JWT_SECRET is too weak. Set a random value of at least {_MIN_SECRET_CHARS} chars, e.g. "
            "python -c \"import secrets; print(secrets.token_urlsafe(48))\""
        )
    models = _csv("GEMINI_MODELS")
    if not models:
        raise ConfigError("GEMINI_MODELS must list at least one model")

    resend_key = os.environ.get("RESEND_API_KEY", "")
    resend_from = _req("RESEND_FROM") if resend_key else ""
    smtp_host = os.environ.get("SMTP_HOST", "").strip()
    early_access_daily_limit = _int_default("EARLY_ACCESS_DAILY_LIMIT", 80)
    early_access_ip_hourly_limit = _int_default("EARLY_ACCESS_IP_HOURLY_LIMIT", 5)
    early_access_ip_minute_limit = _int_default("EARLY_ACCESS_IP_MINUTE_LIMIT", 3)
    early_access_cooldown = _int_default("EARLY_ACCESS_CONFIRMATION_COOLDOWN_MINUTES", 1440)
    early_access_expiry = _int_default("EARLY_ACCESS_CONFIRMATION_EXPIRE_HOURS", 24)
    if min(early_access_daily_limit, early_access_ip_hourly_limit, early_access_ip_minute_limit,
           early_access_cooldown, early_access_expiry) < 1:
        raise ConfigError("Early-access limits, cooldown, and expiry must all be positive integers")

    cors_raw = _req("CORS_ALLOWED_ORIGINS").strip()
    cors = ["*"] if cors_raw == "*" else [o.strip() for o in cors_raw.split(",") if o.strip()]

    return Settings(
        jwt_secret=jwt_secret,
        jwt_algorithm=_req("JWT_ALGORITHM"),
        access_token_expire_minutes=_int("ACCESS_TOKEN_EXPIRE_MINUTES"),
        refresh_token_days=_int("REFRESH_TOKEN_DAYS"),
        google_client_ids=_csv("GOOGLE_CLIENT_IDS", required=False),
        apple_bundle_id=_req("APPLE_BUNDLE_ID"),
        supabase_url=_req("SUPABASE_URL").rstrip("/"),
        supabase_db_url=_req("SUPABASE_DB_URL"),
        supabase_service_key=_req("SUPABASE_SERVICE_KEY"),
        gemini_api_key=_req("GEMINI_API_KEY"),
        gemini_models=models,
        consent_version=_req("CONSENT_VERSION"),
        free_total_analyses=_int("FREE_TOTAL_ANALYSES"),
        pro_term_days=_int("PRO_TERM_DAYS"),
        pro_price_inr=_int("PRO_PRICE_INR"),
        allow_self_serve_pro=_bool("ALLOW_SELF_SERVE_PRO"),
        admin_emails=[e.lower() for e in _csv("ADMIN_EMAILS", required=False)],
        admin_allowed_ips=_csv("ADMIN_ALLOWED_IPS", required=False),
        google_play_package_name=os.environ.get("GOOGLE_PLAY_PACKAGE_NAME", "com.folin.app").strip(),
        google_play_pro_product_id=os.environ.get("GOOGLE_PLAY_PRO_PRODUCT_ID", "").strip(),
        google_play_pro_base_plan_id=os.environ.get("GOOGLE_PLAY_PRO_BASE_PLAN_ID", "").strip(),
        google_play_service_account_json=os.environ.get("GOOGLE_PLAY_SERVICE_ACCOUNT_JSON", "").strip(),
        google_play_rtdn_audience=os.environ.get("GOOGLE_PLAY_RTDN_AUDIENCE", "").strip(),
        google_play_pubsub_service_account_email=os.environ.get(
            "GOOGLE_PLAY_PUBSUB_SERVICE_ACCOUNT_EMAIL", ""
        ).strip().lower(),
        resend_api_key=resend_key,
        resend_from=resend_from,
        app_invite_url=_req("APP_INVITE_URL"),
        early_access_from=os.environ.get("EARLY_ACCESS_FROM", "").strip() or resend_from,
        early_access_confirm_url=os.environ.get("EARLY_ACCESS_CONFIRM_URL", "").strip(),
        early_access_web_url=os.environ.get("EARLY_ACCESS_WEB_URL", "").strip(),
        early_access_daily_limit=early_access_daily_limit,
        early_access_ip_hourly_limit=early_access_ip_hourly_limit,
        early_access_ip_minute_limit=early_access_ip_minute_limit,
        early_access_confirmation_cooldown_minutes=early_access_cooldown,
        early_access_confirmation_expire_hours=early_access_expiry,
        turnstile_secret_key=os.environ.get("TURNSTILE_SECRET_KEY", "").strip(),
        turnstile_expected_hostname=os.environ.get("TURNSTILE_EXPECTED_HOSTNAME", "").strip(),
        turnstile_expected_action=os.environ.get("TURNSTILE_EXPECTED_ACTION", "").strip(),
        resend_webhook_secret=os.environ.get("RESEND_WEBHOOK_SECRET", "").strip(),
        smtp_host=smtp_host,
        smtp_port=_int("SMTP_PORT") if smtp_host else 0,
        smtp_user=_req("SMTP_USER").strip() if smtp_host else "",
        smtp_pass=_req("SMTP_PASS") if smtp_host else "",
        smtp_from=_req("SMTP_FROM").strip() if smtp_host else "",
        expo_access_token=os.environ.get("EXPO_ACCESS_TOKEN", "").strip(),
        firebase_service_account_file=os.environ.get("FIREBASE_SERVICE_ACCOUNT_FILE", "").strip(),
        cors_allowed_origins=cors,
        trusted_proxy_ips=frozenset(_csv("TRUSTED_PROXY_IPS")),
    )


settings = _load()
