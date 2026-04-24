"""HTTP transport-layer surfaces that are not a per-context REST router.

The first tenant is Server-Sent Events (``/w/<slug>/events``, cd-clz9):
a single in-process fan-out that carries every cross-client coherence
signal. See :mod:`app.api.transport.sse`.
"""
