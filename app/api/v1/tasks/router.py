"""Aggregate router for the tasks context."""

from __future__ import annotations

from fastapi import APIRouter

from .comments import router as comments_router
from .evidence import router as evidence_router
from .nl import router as nl_router
from .occurrences import router as occurrences_router
from .schedules import router as schedules_router
from .templates import router as templates_router

router = APIRouter(tags=["tasks"])
router.include_router(templates_router)
router.include_router(schedules_router)
router.include_router(occurrences_router)
router.include_router(nl_router)
router.include_router(comments_router)
router.include_router(evidence_router)

__all__ = ["router"]
