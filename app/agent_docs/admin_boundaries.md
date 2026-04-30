---
slug: admin_boundaries
title: Deployment admin boundaries
summary: Rules for deployment-admin operations and tenant invisibility.
roles: [admin]
capabilities: [chat.admin]
---

# Deployment admin boundaries

Deployment admins manage the installation rather than one tenant's
daily work. The admin surface covers workspaces, deployment settings,
admin membership, audit, usage, chat gateway health, LLM configuration,
and system docs.

Do not reveal whether an admin route, workspace, token, or user exists
to a caller without the relevant deployment grant. Treat missing
deployment authority as not found.

When an operator asks for a change that could affect every workspace,
state the blast radius before acting and require confirmation for
irreversible or security-sensitive changes.
