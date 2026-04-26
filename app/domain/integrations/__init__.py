"""``integrations`` domain context — outbound webhook surface (cd-q885).

The encryption primitive lives in
:mod:`app.adapters.storage.envelope` (the seam other domains consume
via :class:`~app.adapters.storage.ports.EnvelopeEncryptor`); the
delivery dispatcher lives in :mod:`app.domain.integrations.webhooks`.

Repository Protocol is :class:`~app.domain.integrations.ports.WebhookRepository`;
the SA-backed concretion lives at
:mod:`app.adapters.db.integrations.repositories`.

See ``docs/specs/10-messaging-notifications.md`` §"Webhooks (outbound)"
and ``docs/specs/02-domain-model.md`` §"webhook_subscription" /
§"webhook_delivery".
"""

from __future__ import annotations
