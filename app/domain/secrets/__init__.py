"""``secrets`` domain context — repository seam for the §15 ``secret_envelope`` row.

The encryption primitive itself lives in
:mod:`app.adapters.storage.envelope` (the seam other domains consume
via :class:`~app.adapters.storage.ports.EnvelopeEncryptor`); this
context owns only the **persistence** of envelope rows. Both pieces
together implement spec §15 "Secret envelope" / "Key fingerprint".

The repository Protocol is :class:`~app.domain.secrets.ports.SecretEnvelopeRepository`;
the SA-backed concretion lives at
:mod:`app.adapters.db.secrets.repositories`. Per
``docs/specs/01-architecture.md`` §"Boundary rules" rule 4 the
Protocol lives on the domain side; the spec separation between the
encryption seam (``app.adapters.storage.envelope``) and the storage
seam (this package) keeps the rotation worker (cd-rotate-root-key)
free to walk envelope rows without dragging the cipher through the
domain layer.
"""

from __future__ import annotations
