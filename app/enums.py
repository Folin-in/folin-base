"""Closed vocabularies. Anything that used to be a bare string compared with == lives here,
so a typo is an AttributeError at import time instead of a silent false branch at runtime."""
from enum import StrEnum


class Plan(StrEnum):
    FREE = "free"
    PRO = "pro"


class AuthProvider(StrEnum):
    LOCAL = "local"
    GOOGLE = "google"
    APPLE = "apple"
    SUPABASE = "supabase"


class ReportType(StrEnum):
    LAB = "lab"
    OPD = "opd"
    PRESCRIPTION = "prescription"
    IMAGING = "imaging"
    DISCHARGE = "discharge"
    OTHER = "other"
    UNKNOWN = "unknown"

    @classmethod
    def known(cls) -> frozenset["ReportType"]:
        return frozenset(cls)


class BiomarkerStatus(StrEnum):
    NORMAL = "normal"
    HIGH = "high"
    LOW = "low"
    BORDERLINE_HIGH = "borderline_high"
    BORDERLINE_LOW = "borderline_low"


class JobStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCESS = "success"
    ERROR = "error"


class PaymentStatus(StrEnum):
    CREATED = "created"
    PAID = "paid"
    FAILED = "failed"


class PlanAction(StrEnum):
    UPGRADED = "upgraded"
    DOWNGRADED = "downgraded"
    EXPIRED = "expired"
    RENEWED = "renewed"
    REVOKED = "revoked"


class ScreenVerdict(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"


class RejectReason(StrEnum):
    PROMPT_INJECTION = "prompt_injection"
    DISGUISED = "disguised"
    NOT_MEDICAL = "not_medical"


class TrajectoryKind(StrEnum):
    """Which agent produced a row in ai_trajectories."""
    SCREEN = "screen"
    TRANSCRIBE = "transcribe"
    ANALYZE = "analyze"
    VERIFY = "verify"
    REFRESH_ADVICE = "refresh_advice"
    RECONCILE_TIPS = "reconcile_tips"


class Language(StrEnum):
    ENGLISH = "english"
    HINGLISH = "hinglish"
