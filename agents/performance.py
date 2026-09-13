"""Per-AI-call latency budgets. Uploads have no overall elapsed-time cutoff."""
from dataclasses import dataclass


@dataclass(frozen=True)
class CallPolicy:
    thinking: str = "high"
    total_seconds: float = 85
    attempt_seconds: float = 60
    retry_rounds: int = 2
    backoff_seconds: float = 1


DEFAULT_POLICY = CallPolicy()
POLICIES = {
    "screen": CallPolicy("low", 25, 8, retry_rounds=1),
    "transcribe": CallPolicy("low", 36, 12, retry_rounds=1),
    # Interpretation retains a bounded request budget, but saving and queue time
    # are not cancelled by an overall job timer. Verification runs after saving.
    "analyze": CallPolicy("high", 120, 60),
    "verify": CallPolicy("medium", 30, 10, retry_rounds=1),
}


def thinking_config(model: str, level: str) -> dict:
    # generateContent uses levels for Gemini 3 and token budgets for Gemini 2.5.
    # Floating aliases/unknown models keep provider defaults rather than receiving
    # a parameter that may not be supported by the model behind the alias.
    if model.startswith("gemini-3"):
        if model == "gemini-3-pro-preview" and level == "medium":
            level = "high"
        return {"thinkingLevel": level}
    if model.startswith("gemini-2.5"):
        return {"thinkingBudget": {"low": 1024, "medium": 4096, "high": 8192}[level]}
    return {}
