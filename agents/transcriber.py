"""Reads every result row out of a lab document, verbatim. Transcription only.

Split out of the extractor because recall and interpretation compete: asked to write an
explanation and a tip for each marker in the same breath, the model economises by
returning fewer markers. This call has nothing to spend attention on but enumeration.

Fails open - the pipeline can still analyse a document whose transcription came back
empty, it just loses the completeness guarantee for that run.
"""
from agents._agent import Agent
from agents.base import CallMeta

_TASK = ("Transcribe every result row in the attached document, page by page. "
         "Do not interpret, summarise or advise.")
_PAGE_NOTE = " The document is provided as {n} images - treat them as consecutive pages of one report."


class TranscriberAgent(Agent):
    NAME = "transcriber"

    async def run(self, files: list[tuple[str, str]], *,
                  user_id: str | None = None) -> tuple[dict, CallMeta | None]:
        text = _TASK + (_PAGE_NOTE.format(n=len(files)) if len(files) > 1 else "")
        meta = self.meta(user_id=user_id)
        parsed = await self._client.call(system_prompt=self.spec.prompt, user_text=text,
                                         files=files, meta=meta)
        return parsed, meta
