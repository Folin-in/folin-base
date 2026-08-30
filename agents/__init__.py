"""Folin's AI agents. Each is a class over the shared ModelClient, runs the prompt its
prompts/<name>.md declares, and may only reach the tools and subagents that file lists.
See docs/ai-agents.md."""
from agents.auditor import DocumentRejected, OutputAuditor
from agents.extractor import ExtractorAgent
from agents.reconciler import TipsReconciler
from agents.refresher import AdviceRefresher
from agents.screener import ScreenerAgent
from agents.transcriber import TranscriberAgent
from agents.verifier import VerifierAgent

__all__ = [
    "ExtractorAgent", "ScreenerAgent", "TranscriberAgent", "VerifierAgent", "AdviceRefresher", "TipsReconciler",
    "OutputAuditor", "DocumentRejected",
]
