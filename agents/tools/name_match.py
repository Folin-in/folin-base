"""Lenient check that the name printed on a report belongs to the profile it is being
filed under. Any shared token (or substring token) counts; only a complete disjoint -
'Ketan Raj' vs 'Sunita Sharma' - is a mismatch. Missing either name is no judgement."""
import re

from app.constants import NAME_TITLES
from agents.tools.base import Tool

_NON_ALPHA = re.compile(r"[^a-z ]")


class NameMatchTool(Tool):
    name = "name_match"

    @staticmethod
    def tokens(name: str) -> set[str]:
        return {t for t in _NON_ALPHA.sub(" ", name.lower()).split() if len(t) > 1 and t not in NAME_TITLES}

    def run(self, profile_name: str | None, patient_name: str | None) -> bool:
        a, b = self.tokens(profile_name or ""), self.tokens(patient_name or "")
        if not a or not b:
            return True
        if a & b:
            return True
        return any(x in y or y in x for x in a for y in b)
