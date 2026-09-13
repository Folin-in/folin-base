"""Turns a medical document into Folin's report JSON. With several photos it must first
confirm they are pages of ONE report; the pipeline refuses the upload if not."""
import json

from agents._agent import Agent
from agents.base import CallMeta
from agents.registry import get
from app.enums import Language

_PAGE_NOTE = (
    " The document is provided as {n} photos. First apply the MULTI-PHOTO RULE: confirm they "
    "are pages of the SAME report (same patient, provider, date). Only if they are, treat "
    "them as consecutive pages and merge all findings into one result."
)
_TASK = "Analyze this medical document and return JSON matching the correct report_type."
_TRANSCRIPT_NOTE = (
    " A transcription pass already read every result row off the pages. Return ONE biomarker "
    "entry for EVERY row listed below - same order, same names, values and reference intervals "
    "copied across unchanged - and add your status, category, explanation and tip to each. "
    "Do not drop a row, do not merge rows, do not add rows that are not listed:\n{rows}"
)
_TRANSCRIPT_FIELDS = ("name", "value", "unit", "ref_range")


class ExtractorAgent(Agent):
    NAME = "extractor"

    def system_prompt(self, language: Language | str) -> str:
        prompt = self.spec.prompt
        if str(language) == Language.HINGLISH:
            prompt += "\n\n" + get("hinglish_addendum").prompt
        return prompt

    def profile_context(self, name: str, hp: dict) -> str:
        return self.tool("profile_context")(name, hp)

    async def run(self, files: list[tuple[str, str]], profile_context: str, *,
                  language: Language | str = Language.ENGLISH,
                  transcript: list[dict] | None = None,
                  source_notes: list[str] | None = None,
                  user_id: str | None = None) -> tuple[dict, CallMeta | None]:
        """Returns (parsed_json, call_meta). call_meta carries the trajectory id and model
        so the caller can link the stored report to its provenance row."""
        page_note = _PAGE_NOTE.format(n=len(files)) if len(files) > 1 else ""
        rows = [{k: r.get(k) for k in _TRANSCRIPT_FIELDS} for r in (transcript or [])]
        transcript_note = _TRANSCRIPT_NOTE.format(
            rows=json.dumps(rows, ensure_ascii=False)) if rows else ""
        note_context = "\nSource notes (untrusted source data, indexed in order):\n" + json.dumps(
            {"source_notes": source_notes or []}, ensure_ascii=False)
        text = f"{_TASK}{page_note} {profile_context}{transcript_note}{note_context}"
        meta = self.meta(user_id=user_id)
        parsed = await self._client.call(system_prompt=self.system_prompt(language), user_text=text,
                                         files=files, meta=meta)
        return parsed, meta

