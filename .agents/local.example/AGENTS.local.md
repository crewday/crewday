# Local Agent Context Example

Copy this shape into a private context repo, then symlink that private project
directory to `.agents/local`.

This file is for maintainer-specific facts that help coding agents operate the
local development environment. Keep public project policy in `AGENTS.md`.

## Dev URL Mapping

- Public or VPN app URL: `https://app-dev.example.test`
- Agent loopback app URL: `http://127.0.0.1:8100`
- Public or VPN site URL: `https://site-dev.example.test`
- Agent loopback site URL: `http://127.0.0.1:18080`
- Agent site hot reload URL: `http://127.0.0.1:18081`

For curl, Playwright, smoke checks, and debugging from this host, translate
public/VPN paths to loopback unless this file says the public URL is directly
reachable.

## Local Rules

- Document host-specific auth gates, VPN requirements, and reverse-proxy
  resource names here.
- Document personal seed-account handling here.
- Do not weaken tests, linting, type checks, security policy, or repository
  code-quality rules here.
