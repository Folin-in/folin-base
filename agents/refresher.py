"""After a user edits a biomarker value, rewrite that marker's explanation and any tips
linked to it so the advice matches the NEW value. Text-only call. Raises on failure -
the caller decides whether stale advice is acceptable."""
import json

from agents._agent import Agent
from app.enums import Language

_LANG_NOTE = " Write ALL narrative text strictly in this language: {language}."
_MARKER_FIELDS = ("name", "value", "unit", "ref_range", "status")


class AdviceRefresher(Agent):
    NAME = "refresh_advice"

    async def run(self, profile_context: str, marker: dict, linked_suggestions: list[dict], *,
                  language: Language | str = Language.ENGLISH,
                  user_id: str | None = None, report_id: str | None = None) -> dict:
        payload = {
            "marker": {k: marker.get(k) for k in _MARKER_FIELDS},
            "linked_suggestions": [{"title": s.get("title"), "body": s.get("body")} for s in linked_suggestions],
        }
        text = (f"{profile_context}{_LANG_NOTE.format(language=language)}\n"
                f"Corrected marker and its current suggestions:\n{json.dumps(payload, ensure_ascii=False)}")
        return await self._call(text, user_id=user_id, report_id=report_id)
