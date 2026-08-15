"""Every fixed value the code depends on, in one place. Grouped by concern.

Nothing here comes from the environment - deployment-varying values (keys, URLs, prices,
quotas) live in app.config and are read from env with no fallback. These are the values
that are part of the product's definition and would only change with a code change.
"""
from app.enums import BiomarkerStatus, ReportType

# --------------------------------------------------------------------- uploads / analysis
ALLOWED_MIME: dict[str, str] = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
MAX_ANALYZE_FILES = 5
MIN_FILE_BASE64_CHARS = 512               # smaller than this can never be a readable report
MAX_UPLOAD_BASE64_BYTES = 14 * 1024 * 1024  # ~10 MB decoded; reject before decoding
ANALYZE_JOB_TIMEOUT_SECS = 180
ANALYZE_JOB_GRACE_SECS = 30               # a 'processing' row older than timeout+grace is dead
GEMINI_CALL_TIMEOUT_SECS = 120
MODEL_COOLDOWN_SECS = 120                 # a failed model is skipped for this long

# --------------------------------------------------------------------- report semantics
SCORING_TYPES: frozenset[ReportType] = frozenset({ReportType.LAB})
SEVERITY: dict[BiomarkerStatus, int] = {
    BiomarkerStatus.NORMAL: 0,
    BiomarkerStatus.BORDERLINE_HIGH: 1,
    BiomarkerStatus.BORDERLINE_LOW: 1,
    BiomarkerStatus.HIGH: 2,
    BiomarkerStatus.LOW: 2,
}
SUGGESTION_CATEGORIES = frozenset({"nutrition", "movement", "sleep", "supplements", "lifestyle"})
LEGACY_CATEGORY_MAP = {"diet": "nutrition", "exercise": "movement"}
FEEDBACK_ISSUES = frozenset({"wrong_value", "missed_marker", "wrong_status", "bad_tip", "wrong_language", "other"})
RETEST_AFTER_DAYS = 90
COMPARE_ORDER = {"improved": 0, "worsened": 1, "same": 2}
DEFAULT_REPORT_TITLES: dict[ReportType, str] = {
    ReportType.LAB: "Lab report",
    ReportType.OPD: "Clinic visit",
    ReportType.PRESCRIPTION: "Prescription",
    ReportType.IMAGING: "Imaging report",
    ReportType.DISCHARGE: "Discharge summary",
    ReportType.OTHER: "Medical document",
    ReportType.UNKNOWN: "Document",
}

# --------------------------------------------------------------------- patient-name matching
NAME_TITLES = frozenset({"mr", "mrs", "ms", "miss", "mx", "dr", "master", "shri", "smt", "sri", "kumari", "baby"})

# --------------------------------------------------------------------- profile vocabularies
VALID_APPEARANCE = frozenset({"light", "dark", "system"})
VALID_NOTIFICATIONS = frozenset({"off", "weekly", "daily"})
VALID_UNITS = frozenset({"us", "si"})
VALID_REGIONS = frozenset({"india", "other"})
HEALTH_PROFILE_KEYS = frozenset({
    "age", "gender", "weight_kg", "height_cm", "workout", "workout_type", "diet",
    "language", "allergies", "allergies_asked",
})
WORKOUT_LABELS = {
    "cardio": "cardio",
    "strength": "strength training",
    "yoga": "yoga",
    "sports": "sports",
    "mixed": "a mix of workouts",
    "walking": "walking",
}
DIET_LABELS = {"vegetarian": "vegetarian", "non_vegetarian": "non-vegetarian", "vegan": "vegan"}
ALLERGY_MAX_CHARS = 40

# --------------------------------------------------------------------- auth / security
RESET_CODE_TTL_MIN = 15
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"
GOOGLE_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})
APPLE_ISSUER = "https://appleid.apple.com"
SUPABASE_AUDIENCE = "authenticated"
SOCIAL_TOKEN_ALGS = ("RS256",)
SUPABASE_TOKEN_ALGS = ("ES256", "RS256")
USERNAME_MAX_CHARS = 24
USERNAME_PATTERN = r"[^a-zA-Z0-9_\.]"

# --------------------------------------------------------------------- rate limits (per IP)
class RateLimit:
    """(max_hits, window_seconds) per bucket. Sliding window, in-memory."""
    LOGIN = ("login", 15, 300)
    REGISTER = ("register", 10, 600)
    REFRESH = ("refresh", 30, 300)
    SOCIAL_LOGIN = ("social_login", 20, 300)
    FORGOT_PASSWORD = ("forgot", 5, 900)


DAILY_QUOTA_FREE_GENERIC = 20            # non-analyze buckets without a named override
DAILY_QUOTA_OVERRIDES = {
    "family_invite_daily": 20,
    "password_reset_email": 3,
}

# --------------------------------------------------------------------- caches (seconds)
USER_CACHE_TTL = 30.0
REPORTS_CACHE_TTL = 300.0
TIPS_CACHE_TTL = 24 * 3600.0

# --------------------------------------------------------------------- storage
STORAGE_BUCKET = "reports"
STORAGE_FILE_SIZE_LIMIT = 10 * 1024 * 1024
SIGNED_URL_TTL_SECS = 3600

# --------------------------------------------------------------------- payments
RAZORPAY_API = "https://api.razorpay.com/v1"
RAZORPAY_CURRENCY = "INR"
RAZORPAY_TIMEOUT_SECS = 20
