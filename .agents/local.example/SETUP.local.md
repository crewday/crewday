# Local Setup Context Example

Use this file in a private context repo for setup details that are true for one
maintainer environment but not for every open-source contributor.

## Compose Overrides

Private `docker-compose.override.yml` values can set:

```dotenv
CREWDAY_DEV_APP_HOST=app-dev.example.test
CREWDAY_DEV_SITE_HOST=site-dev.example.test
CREWDAY_APP_HOST_PORT=8100
CREWDAY_MAILPIT_SMTP_HOST_PORT=1026
CREWDAY_MAILPIT_UI_HOST_PORT=8026
SITE_WEB_DEV_PORT=18081
CREWDAY_TRAEFIK_MIDDLEWARES=forward-auth@file
```

## Private Notes

- Record reverse proxy, VPN, and auth-gate behavior here.
- Record maintainer-specific signup, activation, and passkey seed recovery
  procedures here.
- Keep secrets in an ignored `.env` or a secret manager, not in this file.
