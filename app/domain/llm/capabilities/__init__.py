"""LLM capability entry points.

Each module exposes a small domain-facing function for one capability key while
sharing the router, budget, and usage-recorder seams from ``app.domain.llm``.
"""

