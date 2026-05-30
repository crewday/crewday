import { describe, expect, it } from "vitest";
import { AUTHENTICATED_ROUTES } from "./_surface";
import {
  FRONTEND_ROUTES,
  FRONTEND_ROUTE_MANIFEST_JSON,
  type FrontendRouteManifestRoute,
  type FrontendRouteName,
} from "./_manifest";

function route(name: FrontendRouteName): FrontendRouteManifestRoute {
  const found = FRONTEND_ROUTE_MANIFEST_JSON.routes.find(
    (entry) => entry.name === name,
  );
  if (!found) {
    throw new Error(`Missing frontend route: ${name}`);
  }
  return found;
}

function templateParams(template: string): string[] {
  return [...template.matchAll(/:([A-Za-z0-9_]+)/g)].map(
    (match) => match[1]!,
  );
}

describe("frontend route manifest", () => {
  it("emits workspace route params and query metadata", () => {
    expect(route("property.detail")).toEqual({
      name: "property.detail",
      scope: "workspace",
      template: "/property/:pid",
      params: [{ name: "pid", required: true, source: "path" }],
      query: [],
      agentLinkable: true,
    });
    expect(route("property.stays")).toEqual({
      name: "property.stays",
      scope: "workspace",
      template: "/property/:pid/stays",
      params: [{ name: "pid", required: true, source: "path" }],
      query: [],
      agentLinkable: true,
    });
    expect(route("property.instructions")).toEqual({
      name: "property.instructions",
      scope: "workspace",
      template: "/property/:pid/instructions",
      params: [{ name: "pid", required: true, source: "path" }],
      query: [],
      agentLinkable: true,
    });
    expect(route("today").query).toEqual([]);
    expect(route("assets.index").query).toEqual([
      { name: "category", required: false },
      { name: "property_id", required: false },
    ]);
    expect(route("audit.index").query).toEqual([
      { name: "actor", required: false },
      { name: "action", required: false },
      { name: "entity", required: false },
      { name: "since", required: false },
      { name: "until", required: false },
    ]);
  });

  it("emits admin route metadata", () => {
    expect(route("admin.agentDocs")).toEqual({
      name: "admin.agentDocs",
      scope: "admin",
      template: "/admin/agent-docs",
      params: [],
      query: [],
      agentLinkable: true,
    });
    expect(route("admin.audit").query).toEqual([
      { name: "actor_kind", required: false },
      { name: "actor_id", required: false },
      { name: "action", required: false },
      { name: "since", required: false },
      { name: "until", required: false },
    ]);
    expect(route("admin.llm.index").agentLinkable).toBe(false);
  });

  it("keeps the authenticated sitemap compatible with the e2e walker", () => {
    expect(AUTHENTICATED_ROUTES[0]).toBe("/today");
    expect(AUTHENTICATED_ROUTES).toContain("/admin/llm");
    expect(AUTHENTICATED_ROUTES).not.toContain("/property/:pid");
  });

  it("keeps route names unique", () => {
    const names = FRONTEND_ROUTE_MANIFEST_JSON.routes.map((entry) => entry.name);

    expect(new Set(names).size).toBe(names.length);
  });

  it("keeps param metadata aligned with path templates", () => {
    for (const entry of FRONTEND_ROUTES) {
      expect(entry.params.map((param) => param.name)).toEqual(
        templateParams(entry.template),
      );
    }
  });

  it("keeps route templates scoped to frontend page paths", () => {
    for (const entry of FRONTEND_ROUTE_MANIFEST_JSON.routes) {
      expect(entry.template).not.toContain("/api/");

      if (entry.scope === "workspace") {
        expect(entry.template).toMatch(/^\/(?!w\/)/);
      } else {
        expect(entry.template).toMatch(/^\/admin(\/|$)/);
      }
    }
  });
});
