"""mail — outbound email rendering for auth and identity flows.

Concrete transport lives under :mod:`app.adapters.mail`. Notification
templates (task-assigned, daily digest, agent message, ...) live with
the notification service under :mod:`app.domain.messaging.templates`
and are rendered by
:class:`app.domain.messaging.notifications.NotificationService`.

Auth-flow templates (magic link, invite, recovery, passkey reset,
email change) live under
:mod:`app.mail.templates` ``auth/`` subdirectory and are rendered by
:func:`app.mail.auth_templates.render_auth_email`.

See ``docs/specs/10-messaging-notifications.md`` §"Email template
system" and ``docs/specs/03-auth-and-tokens.md``.
"""
