"""mail — outbound email templates and rendering.

Concrete transport lives under :mod:`app.adapters.mail`; this package
holds the per-purpose templates and rendering helpers. v1 ships a
minimal ``str.format_map`` wrapper rather than a full templating
engine — a template is a ``.py`` module that exposes ``subject``,
``body_text``, and an optional ``body_html`` as string constants with
``{placeholder}`` slots. A future task will swap in Jinja2 once the
template surface justifies the dependency.

See ``docs/specs/10-messaging-notifications.md`` §"Email".
"""
