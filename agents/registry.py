"""Loads every prompts/*.md at import, parses its frontmatter into an AgentSpec, and
resolves the [product_boundary]-style includes. This is the single source of truth for
what each agent is, which prompt it runs, and which tools and subagents it may reach.

An agent may only call a tool or subagent its .md declares. `AgentSpec.assert_may_call`
enforces that at runtime, so a code change that reaches for an undeclared tool fails
loudly instead of quietly widening an agent's reach.
"""
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from app.enums import TrajectoryKind

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_FRONTMATTER_DELIM = "---"


class AgentKind(StrEnum):
    AGENT = "agent"                # makes a model call
    ORCHESTRATOR = "orchestrator"  # composes other agents, no model call of its own
    FRAGMENT = "fragment"          # reusable prompt text, not callable


class ModelCall(StrEnum):
    DOCUMENT = "document"  # sends file(s) + text
    TEXT = "text"          # text only
    NONE = "none"


class FailMode(StrEnum):
    OPEN = "open"      # on an unparseable reply, let the caller decide
    CLOSED = "closed"  # on an unparseable reply, treat as rejection


class RegistryError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentSpec:
    name: str
    kind: AgentKind
    description: str
    prompt: str                                   # fully resolved (includes inlined)
    model_call: ModelCall = ModelCall.NONE
    tools: frozenset[str] = field(default_factory=frozenset)
    subagents: frozenset[str] = field(default_factory=frozenset)
    output: str = "json"
    trajectory_kind: TrajectoryKind | None = None
    fail_mode: FailMode = FailMode.OPEN

    def assert_may_call(self, *, tool: str | None = None, subagent: str | None = None) -> None:
        if tool is not None and tool not in self.tools:
            raise RegistryError(f"agent '{self.name}' is not declared to use tool '{tool}' - add it to prompts/{self.name}.md")
        if subagent is not None and subagent not in self.subagents:
            raise RegistryError(f"agent '{self.name}' is not declared to call subagent '{subagent}' - add it to prompts/{self.name}.md")


def _parse_list(raw: str) -> frozenset[str]:
    raw = raw.strip()
    if not (raw.startswith("[") and raw.endswith("]")):
        raise RegistryError(f"expected a [list], got {raw!r}")
    inner = raw[1:-1].strip()
    return frozenset(x.strip() for x in inner.split(",") if x.strip()) if inner else frozenset()


def _parse_file(path: Path) -> tuple[dict[str, str], str]:
    text = path.read_text()
    if not text.startswith(_FRONTMATTER_DELIM):
        raise RegistryError(f"{path.name}: missing frontmatter")
    _, fm, body = text.split(_FRONTMATTER_DELIM, 2)
    meta: dict[str, str] = {}
    for line in fm.strip().splitlines():
        if ":" not in line:
            raise RegistryError(f"{path.name}: bad frontmatter line {line!r}")
        k, v = line.split(":", 1)
        meta[k.strip()] = v.strip()
    return meta, body.strip()


def _load_all() -> dict[str, AgentSpec]:
    raw: dict[str, tuple[dict[str, str], str]] = {}
    for path in sorted(PROMPTS_DIR.glob("*.md")):
        meta, body = _parse_file(path)
        name = meta.get("name")
        if name != path.stem:
            raise RegistryError(f"{path.name}: frontmatter name {name!r} must equal the filename")
        raw[name] = (meta, body)

    def resolve(name: str, seen: tuple[str, ...] = ()) -> str:
        if name in seen:
            raise RegistryError(f"include cycle: {' -> '.join(seen + (name,))}")
        meta, body = raw[name]
        parts = []
        for inc in _parse_list(meta.get("includes", "[]")):
            if inc not in raw:
                raise RegistryError(f"{name}.md includes unknown fragment '{inc}'")
            parts.append(resolve(inc, seen + (name,)))
        parts.append(body)
        return "\n\n".join(parts)

    specs: dict[str, AgentSpec] = {}
    for name, (meta, _) in raw.items():
        kind = AgentKind(meta.get("kind", "agent"))
        tk = meta.get("trajectory_kind", "none")
        specs[name] = AgentSpec(
            name=name,
            kind=kind,
            description=meta.get("description", ""),
            prompt=resolve(name),
            model_call=ModelCall(meta.get("model_call", "none")),
            tools=_parse_list(meta.get("tools", "[]")),
            subagents=_parse_list(meta.get("subagents", "[]")),
            output=meta.get("output", "json"),
            trajectory_kind=None if tk == "none" else TrajectoryKind(tk),
            fail_mode=FailMode(meta.get("fail_mode", "open")),
        )

    # Every declared subagent must exist and be callable; every include must be a fragment.
    for spec in specs.values():
        for sub in spec.subagents:
            if sub not in specs:
                raise RegistryError(f"{spec.name} declares unknown subagent '{sub}'")
            if specs[sub].kind == AgentKind.FRAGMENT:
                raise RegistryError(f"{spec.name} lists fragment '{sub}' as a subagent")
    return specs


AGENTS: dict[str, AgentSpec] = _load_all()


def get(name: str) -> AgentSpec:
    try:
        return AGENTS[name]
    except KeyError:
        raise RegistryError(f"no agent named '{name}' - known: {sorted(AGENTS)}") from None
