"""A tool is a named, side-effect-free callable an agent is allowed to reach. The name is
what prompts/*.md declares under `tools:`; `TOOLS` is the lookup the agents use, and the
registry checks every call against the agent's declaration."""
from abc import ABC, abstractmethod
from typing import Any


class Tool(ABC):
    name: str

    @abstractmethod
    def run(self, *args: Any, **kwargs: Any) -> Any: ...

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.run(*args, **kwargs)
