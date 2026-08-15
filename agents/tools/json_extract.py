"""Pulls the JSON object out of a model reply, tolerating markdown fences and any prose
around it. Raises ValueError (never returns a partial dict) when there is no object."""
import json
import re

from agents.tools.base import Tool

_FENCE = re.compile(r"^```(?:json)?|```$", re.MULTILINE)


class JsonExtractTool(Tool):
    name = "json_extract"

    def run(self, text: str) -> dict:
        text = _FENCE.sub("", text.strip()).strip()
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            raise ValueError("No JSON object found in model response")
        return json.loads(text[start:end + 1])
