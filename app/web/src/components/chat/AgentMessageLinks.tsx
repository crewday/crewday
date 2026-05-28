import { Link } from "react-router-dom";
import type { ReactElement } from "react";
import type { AgentMessage } from "@/types/api";
import { frontendRouteByName, type FrontendRoute } from "@/routes/_manifest";

interface RenderableAgentLink {
  href: string;
  label: string;
}

const FALLBACK_ORIGIN = "http://localhost";

export default function AgentMessageLinks({
  message,
}: {
  message: AgentMessage;
}): ReactElement | null {
  const links = safeAgentLinks(message);
  if (links.length === 0) return null;

  return (
    <nav className="agent-message-links" aria-label="Agent suggested links">
      {links.map((link) => (
        <Link
          key={`${link.href}:${link.label}`}
          className="btn btn--ghost btn--sm agent-message-links__link"
          to={link.href}
        >
          {link.label}
        </Link>
      ))}
    </nav>
  );
}

function safeAgentLinks(message: AgentMessage): RenderableAgentLink[] {
  const candidates: unknown[] = [];
  if (Array.isArray(message.links)) candidates.push(...message.links);

  const agentLinks = message.agent_links;
  if (Array.isArray(agentLinks)) {
    candidates.push(...agentLinks);
  } else if (isRecord(agentLinks)) {
    if (Array.isArray(agentLinks.links)) candidates.push(...agentLinks.links);
    if (Array.isArray(agentLinks.items)) {
      for (const item of agentLinks.items) {
        if (isRecord(item) && Array.isArray(item.links)) {
          candidates.push(...item.links);
        }
      }
    }
  }

  const seen = new Set<string>();
  const links: RenderableAgentLink[] = [];
  for (const candidate of candidates) {
    const link = safeAgentLink(candidate);
    if (link === null) continue;
    const key = `${link.href}:${link.label}`;
    if (seen.has(key)) continue;
    seen.add(key);
    links.push(link);
  }
  return links;
}

function safeAgentLink(raw: unknown): RenderableAgentLink | null {
  if (!isRecord(raw)) return null;
  const rawLabel = raw.label;
  const rawHref = raw.href;
  const rawRoute = raw.route;
  if (
    typeof rawLabel !== "string" ||
    typeof rawHref !== "string" ||
    typeof rawRoute !== "string"
  ) {
    return null;
  }
  const label = rawLabel.trim();
  const route = frontendRouteByName(rawRoute.trim());
  if (route === undefined || !route.agentLinkable) return null;
  const href = safeAgentHref(rawHref, route);
  if (label === "" || href === null) return null;
  return { href, label };
}

function safeAgentHref(rawHref: string, route: FrontendRoute): string | null {
  const href = rawHref.trim();
  if (!href.startsWith("/") || href.startsWith("//") || href.includes("\\")) return null;

  try {
    const baseOrigin =
      typeof window === "undefined" ? FALLBACK_ORIGIN : window.location.origin;
    const parsed = new URL(href, baseOrigin);
    if (parsed.origin !== baseOrigin) return null;
    if (parsed.hash !== "") return null;
    if (!matchesRoute(parsed.pathname, parsed.searchParams, route)) return null;
    return `${parsed.pathname}${parsed.search}`;
  } catch {
    return null;
  }
}

function matchesRoute(
  pathname: string,
  searchParams: URLSearchParams,
  route: FrontendRoute,
): boolean {
  const pathSegments = pathSegmentsFor(pathname);
  if (pathSegments === null) return false;

  let routePathSegments: string[];
  if (route.scope === "workspace") {
    if (pathSegments[0] !== "w" || !isSafePathSegment(pathSegments[1])) return false;
    routePathSegments = pathSegments.slice(2);
  } else if (route.scope === "admin") {
    routePathSegments = pathSegments;
  } else {
    return false;
  }

  const templateSegments = route.template.split("/").filter(Boolean);
  if (routePathSegments.length !== templateSegments.length) return false;
  for (let index = 0; index < templateSegments.length; index += 1) {
    const segment = routePathSegments[index];
    const template = templateSegments[index];
    if (template === undefined) return false;
    if (!isSafePathSegment(segment)) return false;
    if (!template.startsWith(":") && decodeURIComponent(segment) !== template) {
      return false;
    }
  }

  const allowedQuery = new Set(route.query.map((entry) => entry.name));
  const seenQuery = new Set<string>();
  for (const key of searchParams.keys()) {
    if (!allowedQuery.has(key) || seenQuery.has(key)) return false;
    seenQuery.add(key);
  }
  return true;
}

function pathSegmentsFor(pathname: string): string[] | null {
  const parts = pathname.split("/");
  if (parts[0] !== "" || parts.slice(1).some((part) => part === "")) return null;
  return parts.slice(1);
}

function isSafePathSegment(segment: string | undefined): segment is string {
  if (segment === undefined || segment === "") return false;
  const decoded = decodeURIComponent(segment);
  return (
    decoded !== "." &&
    decoded !== ".." &&
    decoded.toLowerCase() !== "api" &&
    !decoded.includes("/") &&
    !decoded.includes("\\")
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
