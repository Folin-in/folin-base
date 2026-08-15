"""Pro-only second pass: re-reads the document and cross-checks every extracted biomarker
(value, unit, status vs printed range). Returns mismatches; empty means clean."""
import json

from agents._agent import Agent
from agents.base import CallMeta

_TASK = "Verify this extraction against the attached document:\n"
_FIELDS = ("name", "value", "unit", "ref_range", "status")


class VerifierAgent(Agent):
    NAME = "verifier"

    async def run(self, files: list[tuple[str, str]], biomarkers: list[dict], *,
                  user_id: str | None = None, report_id: str | None = None
                  ) -> tuple[list[dict], CallMeta | None]:
        """Returns (mismatches, call_meta). Empty mismatches means the extraction checked out."""
        compact = [{k: b.get(k) for k in _FIELDS} for b in biomarkers]
        meta = self.meta(user_id=user_id, report_id=report_id)
        parsed = await self._client.call(system_prompt=self.spec.prompt,
                                         user_text=_TASK + json.dumps(compact, ensure_ascii=False),
                                         files=files, meta=meta)
        return [m for m in (parsed.get("mismatches") or []) if isinstance(m, dict) and m.get("name")], meta
