"""Messaging API subrouters shared by v1 mounts."""

from __future__ import annotations

from app.api.messaging.channels import build_channels_router

__all__ = ["build_channels_router"]
