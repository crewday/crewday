"""Factory stubs for the ``tasks`` context.

Real factories land with this context's domain + DB models
(tracked as a future cd-* task). Until then, ``import``-ing this
module is a no-op — tests that need a tasks row will import
factories that do not yet exist here, which is the honest state.

See ``docs/specs/17-testing-quality.md`` §"Unit".
"""

from __future__ import annotations

# Placeholder — will grow when tasks's domain + DB models land.

__all__: list[str] = []
