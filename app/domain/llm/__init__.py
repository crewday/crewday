"""LLM context — model router, agent runtime, approvals, preferences, budget.

See docs/specs/11-llm-and-agents.md.
"""

from __future__ import annotations

from app.domain.llm.prompts import get_active_prompt

__all__ = ["get_active_prompt"]
