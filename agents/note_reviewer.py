"""One-time review for older reports; never called on normal report reads."""
import json

from agents._agent import Agent
from agents.base import BadModelReply
from services.report_notes import detail_sections, select_report_notes


class NoteReviewer(Agent):
    NAME = "note_reviewer"

    async def run(self, notes: list[str], report: dict, *, user_id: str, report_id: str):
        sections = detail_sections(report)
        result = await self._call(json.dumps({"source_notes": notes, "detail_sections": sections}, ensure_ascii=False),
                                  user_id=user_id, report_id=report_id)
        review = select_report_notes(notes, result.get("note_decisions") if isinstance(result, dict) else None, sections)
        if not review.reviewed:
            raise BadModelReply("Incomplete note relevance review; saved notes were not changed")
        return review
