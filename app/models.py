from typing import List, Literal, Optional

from pydantic import BaseModel, EmailStr, Field, model_validator


class BaseDocument(BaseModel):
    id: Optional[str] = None

    @classmethod
    def from_row(cls, row: dict):
        return cls.model_validate({**row, "id": str(row["id"])})


# ---------- Auth ----------
class RegisterRequest(BaseModel):
    email: EmailStr
    username: str = Field(min_length=3, max_length=30, pattern=r"^[a-zA-Z0-9_\.]+$")
    password: str = Field(min_length=6, max_length=128)
    name: str = Field(default="", max_length=80)
    # Explicit 18+ / health-data consent (DPDP, WA MHMD). Must be true to register.
    consent: bool = False


class LoginRequest(BaseModel):
    identifier: str = Field(min_length=3)  # email or username
    password: str = Field(min_length=1)


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    email: EmailStr
    code: str = Field(min_length=4, max_length=12)
    new_password: str = Field(min_length=6, max_length=128)


# ---------- Health profile ----------
WORKOUT_TYPES = {"walking", "cardio", "strength", "yoga", "sports", "mixed"}
DIETS = {"vegetarian", "non_vegetarian", "vegan"}
GENDERS = {"male", "female"}
LANGUAGES = {"english", "hinglish", "hindi", "tamil", "telugu", "bengali", "marathi", "gujarati", "punjabi", "spanish"}


class HealthProfileUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=80)
    email: Optional[EmailStr] = None
    username: Optional[str] = Field(default=None, min_length=3, max_length=30, pattern=r"^[a-zA-Z0-9_\.]+$")
    age: Optional[int] = Field(default=None, ge=1, le=120)
    gender: Optional[str] = None
    weight_kg: Optional[float] = Field(default=None, ge=10, le=400)
    height_cm: Optional[float] = Field(default=None, ge=50, le=260)
    workout: Optional[bool] = None
    workout_type: Optional[str] = None
    diet: Optional[str] = None
    language: Optional[str] = None  # see LANGUAGES
    # spec: strict allergy lock. Free-form list; each entry capped at 40 chars, up to 30 entries.
    allergies: Optional[List[str]] = Field(default=None, max_length=30)
    # Onboarding flag - marks that user has explicitly seen/answered the allergies prompt (even to skip).
    allergies_asked: Optional[bool] = None


def public_user(user: dict) -> dict:
    """Strip sensitive fields before returning a user to the client."""
    return {
        "user_id": user["user_id"],
        "email": user["email"],
        "username": user.get("username"),
        "name": user.get("name") or "",
        "picture": user.get("picture"),
        "auth_provider": user.get("auth_provider", "local"),
        "health_profile": user.get("health_profile") or {},
        "preferences": user.get("preferences") or default_preferences(),
        "consented_at": user.get("consented_at"),
        "created_at": user.get("created_at"),
    }


DEFAULT_PREFS = {
    "appearance": "light",           # light | dark | system
    "notifications": "weekly",       # off | weekly | daily
    "units": "us",                   # us (mg/dL) | si (mmol/L) - lab values only
    "region": "india",               # india | other - drives weight/height input units
    "plan": "free",                  # free | pro
}


def default_preferences() -> dict:
    return dict(DEFAULT_PREFS)


# ---------- Preferences ----------
class PreferencesUpdate(BaseModel):
    appearance: Optional[str] = None
    notifications: Optional[str] = None
    units: Optional[str] = None
    region: Optional[str] = None


class PushTokenRequest(BaseModel):
    token: str = Field(min_length=20, max_length=4096,
                       pattern=r"^[A-Za-z0-9_:\[\]-]+$")
    provider: Literal["expo", "fcm"] = "expo"
    platform: Literal["android", "ios"]
    timezone: str = Field(default="UTC", min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_provider(self):
        import re
        expo = bool(re.fullmatch(r"(Exponent|Expo)PushToken\[[A-Za-z0-9_-]+\]", self.token))
        if self.provider == "expo" and not expo:
            raise ValueError("Expected an Expo token")
        if self.provider == "fcm" and (self.platform != "android" or not re.fullmatch(r"[A-Za-z0-9_:-]+", self.token) or expo):
            raise ValueError("Expected an Android FCM token")
        return self


class PushTokenDeleteRequest(BaseModel):
    token: str = Field(min_length=20, max_length=4096, pattern=r"^[A-Za-z0-9_:\[\]-]+$")


PLANS = {"free", "pro"}


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=6, max_length=128)


class ChangeEmailRequest(BaseModel):
    password: str = Field(min_length=1, max_length=128)
    new_email: EmailStr


class PlanChangeRequest(BaseModel):
    plan: str = Field(min_length=1)


# ---------- Member profiles () ----------
MEMBER_RELATIONS = {"self", "partner", "parent", "child", "sibling", "family", "other"}


class MemberUpsertRequest(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    relation: str = Field(default="family", max_length=20)
    age: Optional[int] = Field(default=None, ge=0, le=120)
    gender: Optional[str] = None
    allergies: Optional[List[str]] = Field(default=None, max_length=30)


# ---------- Family ----------
RELATIONSHIPS = {"partner", "parent", "child", "sibling", "friend", "family"}


class FamilyInviteRequest(BaseModel):
    email: EmailStr
    relationship: str = Field(default="family", max_length=20)


# ---------- Web early access ----------
class EarlyAccessRequest(BaseModel):
    email: EmailStr
    turnstile_token: str = Field(min_length=1, max_length=2048)
    # Hidden field. Real users leave it empty; simple form bots commonly fill it.
    website: str = Field(default="", max_length=200)


# ---------- Reports ----------
class Biomarker(BaseModel):
    name: str
    value: str = ""
    unit: str = ""
    ref_range: str = ""
    status: str = "normal"
    category: str = "Other"
    explanation: str = ""
    tip: str = ""
    # () Marks values the user corrected after AI OCR.
    user_edited: bool = False
    # Accuracy features: set by sanity guardrails (free) or the Pro AI self-check.
    needs_verification: bool = False
    verification_note: Optional[str] = None
    # First-edit snapshot: the AI-read value/status survive every user edit.
    original_value: Optional[str] = None
    original_status: Optional[str] = None


class ReportFeedbackIn(BaseModel):
    correct: bool
    issues: List[str] = []
    note: str = Field(default="", max_length=500)
    biomarker_index: Optional[int] = Field(default=None, ge=0)


class BiomarkerPatch(BaseModel):
    index: int = Field(ge=0)
    value: Optional[str] = None
    status: Optional[str] = None  # normal | high | low | borderline_high | borderline_low


class Suggestion(BaseModel):
    title: str
    body: str
    category: str = "lifestyle"  # nutrition | movement | sleep | supplements | lifestyle
    linked_marker: Optional[str] = None  # e.g. "LDL", "Vitamin D"


class Medication(BaseModel):
    name: str
    dose: Optional[str] = None
    schedule: Optional[str] = None
    duration: Optional[str] = None
    purpose: Optional[str] = None


# Allowed report types the AI can classify a document as.
REPORT_TYPES = {"lab", "opd", "prescription", "imaging", "discharge", "other"}


class Report(BaseDocument):
    user_id: str = ""
    # which member (sub-profile) this report belongs to. "self" for the user.
    member_id: str = "self"
    filename: str = ""
    mime_type: str = ""
    # Path of the original file in Supabase Storage (None for legacy/failed uploads)
    storage_path: Optional[str] = None
    # sha256 of the uploaded bytes - blocks accidental duplicate uploads of the same document
    file_hash: Optional[str] = None
    uploaded_at: str = ""
    # Universal medical document type - defaults to "lab" for backward compat.
    report_type: str = "lab"
    # User-editable custom title (falls back to a type-based default in the UI).
    title: Optional[str] = None
    report_date: Optional[str] = None
    patient_name: Optional[str] = None
    # True when the patient name on the document doesn't look like the profile it was uploaded to.
    name_mismatch: bool = False
    # lab_name doubles as the provider name (hospital / clinic / doctor) for non-lab types.
    lab_name: Optional[str] = None
    summary: str = ""
    overall_assessment: str = ""
    positives: List[str] = []
    concerns: List[str] = []
    # Verbatim interpretive comments printed by the lab. Not Folin's words - never edited.
    lab_notes: List[str] = []
    suggestions: List[Suggestion] = []
    # Lab-report fields
    biomarkers: List[Biomarker] = []
    health_score: int = 0
    normal_count: int = 0
    attention_count: int = 0
    total_count: int = 0
    # OPD / clinical-visit fields
    chief_complaint: Optional[str] = None
    exam_findings: List[str] = []
    diagnoses: List[str] = []
    referrals: List[str] = []
    # Prescription / treatment-plan fields (may also appear on OPD & discharge)
    medications: List[Medication] = []
    # Imaging fields
    modality: Optional[str] = None
    body_area: Optional[str] = None
    findings: List[str] = []
    impression: Optional[str] = None
    # Discharge summary fields
    admission_date: Optional[str] = None
    discharge_date: Optional[str] = None
    procedures: List[str] = []
    follow_up: Optional[str] = None


class AnalyzeFileIn(BaseModel):
    filename: str = ""
    mime_type: str
    file_base64: str


class AnalyzeRequest(BaseModel):
    # Legacy single-file fields (still accepted). New clients send `files` (1-5 items).
    filename: str = ""
    mime_type: str = ""
    file_base64: str = ""
    files: Optional[List[AnalyzeFileIn]] = None
    member_id: Optional[str] = None  # spec #11 - defaults to "self" on the server


class ReportTitleUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=80)


# ---------- Auth request bodies ----------
class RefreshRequest(BaseModel):
    refresh_token: str


class GoogleLoginRequest(BaseModel):
    id_token: str


class AppleLoginRequest(BaseModel):
    identity_token: str
    name: str | None = None  # Apple sends the name only on the FIRST sign-in


class SupabaseSyncRequest(BaseModel):
    """Only consulted when the identity is brand new; ignored for an existing account."""
    name: str | None = None
    username: str | None = None
    consent: bool = False


class GooglePlayVerifyRequest(BaseModel):
    purchase_token: str = Field(min_length=10, max_length=4096)


class GooglePlayRtdnMessage(BaseModel):
    data: str = Field(min_length=1)
    message_id: Optional[str] = Field(default=None, alias="messageId")


class GooglePlayRtdnEnvelope(BaseModel):
    message: GooglePlayRtdnMessage


# ---------- Admin bodies ----------
class GrantProRequest(BaseModel):
    identifier: str = Field(min_length=1, max_length=128, description="Email, username, or user_id")
    days: int = Field(default=365, ge=1, le=3650, description="Duration in days (default: 365)")


class BlockUserRequest(BaseModel):
    identifier: str = Field(min_length=1, max_length=128, description="Email, username, or user_id")
    reason: Optional[str] = Field(default="", max_length=255, description="Reason for suspension")


class UnblockUserRequest(BaseModel):
    identifier: str = Field(min_length=1, max_length=128, description="Email, username, or user_id")


