"""Deterministic post-check of the extractor's JSON. No model call - a regex pass, so it
cannot itself be injected and behaves identically every run. Catches what the screener
missed: an obeyed instruction that leaked into the output, or a wellness-boundary breach.

DocumentRejected is raised by both this and the screener; the pipeline turns it into a
422 and refunds the quota."""
import re

from agents._agent import Agent
from app.enums import ReportType

_INSTRUCTION_PATTERNS = (
    r"\bignore (all |the |your )?(previous|prior|above|earlier) (instructions?|prompts?|rules?)\b",
    r"\bdisregard (all |the |your )?(previous|prior|above|earlier)\b",
    r"\byou are (now )?(an? |the )?(ai|assistant|language model|llm|chatbot|different)\b",
    r"\bsystem prompt\b",
    r"\bas an ai\b",
    r"\bnew instructions?\b",
    r"\bdo not (tell|inform|mention|reveal)\b.*\b(user|patient|anyone)\b",
    r"\breport (all|every|each) (values?|results?) as (normal|fine|healthy)\b",
    r"\bmark (everything|all|every\w*) (as )?(normal|healthy|fine)\b",
    r"\b(stop|start|increase|decrease|double|halve|skip) (taking )?(your |the |all )?[a-z]*\s?"
    r"(dose|dosage|medication|medicine|tablets?|pills?|insulin|metformin|statins?)\b",
)
INSTRUCTION_RE = re.compile("|".join(_INSTRUCTION_PATTERNS), re.IGNORECASE)

# The wellness-not-medical-device boundary, as things FOLIN must never claim to do.
BANNED_VERB_RE = re.compile(
    r"\b(we|folin|this app|i)\s+(have\s+)?(diagnos\w*|detect\w*|screen\w*\s+for|prescrib\w*|cur\w*|treat\w*)\b"
    r"|\byou (have|are suffering from|are diagnosed with)\b"
    r"|\bmedical advice\b",
    re.IGNORECASE,
)

# Model-authored text, where an obeyed injection or a boundary breach would surface.
AUTHORED_FIELDS = frozenset({"summary", "overall_assessment", "positives", "concerns", "suggestions",
                             "impression", "follow_up", "pages_issue"})
# Transcribed from the document: quoting the DOCTOR's diagnosis here is transcription.
TRANSCRIBED_FIELDS = frozenset({"diagnoses", "chief_complaint", "exam_findings", "medications",
                                "findings", "procedures"})

MSG_INJECTION = ("This file contains text that looks like instructions to an AI rather than a medical "
                 "report, so we've stopped here. If this is a genuine report, please upload a clean photo of it.")
MSG_DISGUISED = ("This looks like it's formatted as a report but doesn't read like a genuine medical "
                 "document, so we've stopped here. Please upload the original report.")
MSG_NOT_MEDICAL = "This doesn't look like a medical document. Try a different file."
MSG_UNVERIFIABLE = "We couldn't verify this file. Please try a clearer photo of the report."
MSG_INTERNAL = "Something went wrong reading this report. Please try again in a moment."
_EVIDENCE_WINDOW = 40


class DocumentRejected(Exception):
    """A document that must not become a report. `user_message` is safe to show."""

    def __init__(self, reason: str, user_message: str, evidence: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.user_message = user_message
        self.evidence = evidence


def _walk_strings(value, path=""):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _walk_strings(v, f"{path}.{k}" if path else k)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _walk_strings(v, f"{path}[{i}]")


def _snippet(text: str, m: re.Match) -> str:
    return f"…{text[max(0, m.start() - _EVIDENCE_WINDOW): m.end() + _EVIDENCE_WINDOW]}…"


class OutputAuditor(Agent):
    NAME = "auditor"

    def run(self, parsed: dict) -> None:
        """Raises DocumentRejected; returns None when the output is clean."""
        for path, text in _walk_strings(parsed):
            top = path.split(".")[0].split("[")[0]
            if m := INSTRUCTION_RE.search(text):
                raise DocumentRejected("output_instruction_leak", MSG_INJECTION, f"{path}: {_snippet(text, m)}")
            if top in AUTHORED_FIELDS and (b := BANNED_VERB_RE.search(text)):
                raise DocumentRejected("output_boundary_violation", MSG_INTERNAL, f"{path}: {_snippet(text, b)}")
        rt = str(parsed.get("report_type") or "").lower()
        if rt and rt not in ReportType.known():
            raise DocumentRejected("output_unknown_type", MSG_INTERNAL, f"report_type={rt!r}")
