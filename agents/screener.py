"""Runs BEFORE the extractor. A separate model call with no extraction task, so an
instruction inside the file has nothing to hijack. One question: is this a genuine
medical document? Fails closed - an unparseable verdict is a rejection."""
from agents._agent import Agent
from agents.auditor import (MSG_DISGUISED, MSG_INJECTION, MSG_NOT_MEDICAL, MSG_UNVERIFIABLE,
                            DocumentRejected)
from app.enums import RejectReason, ScreenVerdict

_TASK = ("Screen the attached file(s). Do NOT extract any values or summarize the content. "
         "Answer only the screening questions and return the JSON described.")
_MESSAGES = {
    RejectReason.PROMPT_INJECTION: MSG_INJECTION,
    RejectReason.DISGUISED: MSG_DISGUISED,
    RejectReason.NOT_MEDICAL: MSG_NOT_MEDICAL,
}
_EVIDENCE_MAX = 300


class ScreenerAgent(Agent):
    NAME = "screener"

    async def run(self, files: list[tuple[str, str]], *, user_id: str | None = None) -> dict:
        """Returns the verdict dict on accept; raises DocumentRejected on reject."""
        verdict = await self._call(_TASK, files, user_id=user_id)
        if not isinstance(verdict, dict) or "verdict" not in verdict:
            raise DocumentRejected("screen_unparseable", MSG_UNVERIFIABLE, str(verdict)[:_EVIDENCE_MAX])
        if str(verdict.get("verdict") or "").strip().lower() == ScreenVerdict.REJECT:
            why = str(verdict.get("reason") or "").strip().lower()
            try:
                reason = RejectReason(why)
                msg = _MESSAGES[reason]
            except ValueError:
                reason, msg = why or "reject", MSG_NOT_MEDICAL
            raise DocumentRejected(f"screen_{reason}", msg, str(verdict.get("evidence") or "")[:_EVIDENCE_MAX])
        return verdict
