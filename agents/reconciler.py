"""Cross-report conflict check on the aggregated tips feed. Returns the indices to drop
(the older side of each genuine contradiction). Raises on failure - the caller fails
open and serves the unfiltered list."""
import json

from agents._agent import Agent


class TipsReconciler(Agent):
    NAME = "reconcile_tips"

    async def run(self, profile_context: str, tips: list[dict], *, user_id: str | None = None) -> list[int]:
        numbered = [
            {"i": i, "title": t.get("title"), "body": t.get("body"),
             "linked_marker": t.get("linked_marker"), "from": t.get("report_date")}
            for i, t in enumerate(tips)
        ]
        parsed = await self._call(f"{profile_context}\nTips:\n{json.dumps(numbered, ensure_ascii=False)}",
                                  user_id=user_id)
        return [i for i in (parsed.get("drop") or []) if isinstance(i, int) and 0 <= i < len(tips)]
