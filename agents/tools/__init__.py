"""Every tool an agent may reach, keyed by the name prompts/*.md declares."""
from agents.tools.base import Tool
from agents.tools.json_extract import JsonExtractTool
from agents.tools.name_match import NameMatchTool
from agents.tools.profile_context import ProfileContextTool
from agents.tools.sanity_bounds import SanityBoundsTool

TOOLS: dict[str, Tool] = {
    t.name: t for t in (ProfileContextTool(), JsonExtractTool(), SanityBoundsTool(), NameMatchTool())
}

__all__ = ["TOOLS", "Tool", "ProfileContextTool", "JsonExtractTool", "SanityBoundsTool", "NameMatchTool"]
