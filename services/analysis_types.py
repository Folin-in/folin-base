"""The pipeline's public contract: what it raises and what it returns. Kept apart from the
pipeline itself so callers (the job runner, the router) depend on this small module, not
on the orchestration."""
from dataclasses import dataclass


class AnalysisRejected(Exception):
    """The user's file is the problem (mixed pages, injection, disguise). 422, refund."""
    def __init__(self, user_message: str, reason: str = "", evidence: str = ""):
        super().__init__(reason or user_message)
        self.user_message, self.reason, self.evidence = user_message, reason, evidence


class UnsupportedDocument(Exception):
    """Not a medical document. 400 - the analysis was done, so it still counts."""
    def __init__(self, user_message: str):
        super().__init__(user_message)
        self.user_message = user_message


class InvalidUpload(Exception):
    """Bad or oversized bytes. `status` is the HTTP code the router should use."""
    def __init__(self, user_message: str, status: int = 400):
        super().__init__(user_message)
        self.user_message, self.status = user_message, status


@dataclass
class AnalysisResult:
    report_id: str
    report: dict
    xp_earned: int
    new_badges: list[dict]
    profile: dict
