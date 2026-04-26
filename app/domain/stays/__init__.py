"""Stays context — reservations, iCal feeds, stay task bundles, guest welcome.

See docs/specs/04-properties-and-stays.md.
"""

from app.domain.stays.guest_link_service import (
    AccessRecord,
    ChecklistItem,
    GuestAsset,
    GuestLinkGone,
    GuestLinkGoneReason,
    GuestLinkNotFound,
    GuestLinkRead,
    ResolvedGuestLink,
    ResolveResult,
    SettingsResolver,
    WelcomeBundle,
    WelcomeMergeInput,
    WelcomeResolver,
    mint_link,
    record_access,
    resolve_link,
    revoke_link,
)

__all__ = [
    "AccessRecord",
    "ChecklistItem",
    "GuestAsset",
    "GuestLinkGone",
    "GuestLinkGoneReason",
    "GuestLinkNotFound",
    "GuestLinkRead",
    "ResolveResult",
    "ResolvedGuestLink",
    "SettingsResolver",
    "WelcomeBundle",
    "WelcomeMergeInput",
    "WelcomeResolver",
    "mint_link",
    "record_access",
    "resolve_link",
    "revoke_link",
]
