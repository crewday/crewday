"""LLM capability entry points.

Each module exposes a small domain-facing function for one capability key while
sharing the router, budget, and usage-recorder seams from ``app.domain.llm``.
"""

from app.domain.llm.capabilities.digest_gen import (
    DIGEST_COMPOSE_CAPABILITY,
    DigestComposeContext,
    DigestProse,
    compose,
)

__all__ = [
    "DIGEST_COMPOSE_CAPABILITY",
    "DigestComposeContext",
    "DigestProse",
    "compose",
]
