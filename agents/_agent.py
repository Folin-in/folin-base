"""Shared base: binds an agent to its registry spec, exposes only its declared tools, and
routes every model call through the one client with the right trajectory kind."""
from agents.base import CallMeta, ModelClient, client as default_client
from agents.registry import AgentSpec, get
from agents.tools import TOOLS, Tool


class Agent:
    NAME: str  # subclasses set this; must match a prompts/<NAME>.md

    def __init__(self, client: ModelClient | None = None):
        self.spec: AgentSpec = get(self.NAME)
        self._client = client or default_client

    def tool(self, name: str) -> Tool:
        self.spec.assert_may_call(tool=name)
        return TOOLS[name]

    def meta(self, user_id: str | None = None, report_id: str | None = None) -> CallMeta | None:
        if self.spec.trajectory_kind is None:
            return None
        return CallMeta(kind=self.spec.trajectory_kind, user_id=user_id, report_id=report_id)

    async def _call(self, user_text: str, files: list[tuple[str, str]] = (), *,
                    system_prompt: str | None = None, user_id: str | None = None,
                    report_id: str | None = None) -> dict:
        return await self._client.call(
            system_prompt=system_prompt or self.spec.prompt, user_text=user_text,
            files=files, meta=self.meta(user_id, report_id),
        )
