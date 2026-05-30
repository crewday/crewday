/**
 * Named frontend route manifest for agent handoff links.
 *
 * This manifest describes SPA page destinations by stable semantic name.
 * Agent link metadata, backend checks, and CLI renderers refer to these
 * names instead of embedding raw hrefs. Workspace entries stay relative to
 * `/w/<slug>` here; callers that render a concrete link are responsible for
 * adding the active workspace slug.
 *
 * This is not `cli/crewday/_surface.json`. The CLI surface files describe
 * API/CLI operations generated from OpenAPI. This frontend manifest describes
 * navigable React pages and is emitted by Vite as `dist/_routes.json`.
 */

const FRONTEND_ROUTE_MANIFEST_VERSION = 1;

export type FrontendRouteScope = "workspace" | "admin";

export type FrontendRouteParamMetadata = {
  readonly name: string;
  readonly required: true;
  readonly source: "path";
};

export type FrontendRouteQueryMetadata = {
  readonly name: string;
  readonly required: false;
};

export type FrontendRouteDefinition<
  Name extends string = string,
  Template extends string = string,
> = {
  readonly name: Name;
  readonly scope: FrontendRouteScope;
  readonly template: Template;
  readonly params: readonly FrontendRouteParamMetadata[];
  readonly query: readonly FrontendRouteQueryMetadata[];
  readonly agentLinkable: boolean;
  readonly authenticatedSurface: boolean;
};

export type FrontendRouteManifestRoute = Omit<
  FrontendRouteDefinition,
  "authenticatedSurface"
>;

export type FrontendRouteManifestJson = {
  readonly version: typeof FRONTEND_ROUTE_MANIFEST_VERSION;
  readonly routes: readonly FrontendRouteManifestRoute[];
};

type RouteConfig<Name extends string, Template extends string> = {
  readonly name: Name;
  readonly scope: FrontendRouteScope;
  readonly template: Template;
  readonly params?: readonly string[];
  readonly query?: readonly string[];
  readonly agentLinkable?: boolean;
  readonly authenticatedSurface?: boolean;
};

function route<const Name extends string, const Template extends string>(
  config: RouteConfig<Name, Template>,
): FrontendRouteDefinition<Name, Template> {
  return {
    name: config.name,
    scope: config.scope,
    template: config.template,
    params: (config.params ?? []).map((name) => ({
      name,
      required: true,
      source: "path",
    })),
    query: (config.query ?? []).map((name) => ({
      name,
      required: false,
    })),
    agentLinkable: config.agentLinkable ?? true,
    authenticatedSurface: config.authenticatedSurface ?? false,
  };
}

export const FRONTEND_ROUTES = [
  route({
    name: "today",
    scope: "workspace",
    template: "/today",
    authenticatedSurface: true,
  }),
  route({
    name: "schedule.index",
    scope: "workspace",
    template: "/schedule",
    authenticatedSurface: true,
  }),
  route({
    name: "myExpenses.index",
    scope: "workspace",
    template: "/my/expenses",
    authenticatedSurface: true,
  }),
  route({
    name: "profile.me",
    scope: "workspace",
    template: "/me",
    authenticatedSurface: true,
  }),
  route({
    name: "scheduler.index",
    scope: "workspace",
    template: "/scheduler",
    authenticatedSurface: true,
  }),
  route({
    name: "history.index",
    scope: "workspace",
    template: "/history",
    query: ["tab"],
    authenticatedSurface: true,
  }),
  route({
    name: "issues.new",
    scope: "workspace",
    template: "/issues/new",
    authenticatedSurface: true,
  }),
  route({
    name: "chat.index",
    scope: "workspace",
    template: "/chat",
    authenticatedSurface: true,
  }),
  route({
    name: "asset.scan",
    scope: "workspace",
    template: "/asset/scan",
    authenticatedSurface: true,
  }),
  route({
    name: "approvals.index",
    scope: "workspace",
    template: "/approvals",
    authenticatedSurface: true,
  }),
  route({
    name: "dashboard",
    scope: "workspace",
    template: "/dashboard",
    authenticatedSurface: true,
  }),
  route({
    name: "leaves.index",
    scope: "workspace",
    template: "/leaves",
    authenticatedSurface: true,
  }),
  route({
    name: "settings.workspace",
    scope: "workspace",
    template: "/settings",
    authenticatedSurface: true,
  }),
  route({
    name: "webhooks.index",
    scope: "workspace",
    template: "/webhooks",
    authenticatedSurface: true,
  }),
  route({
    name: "chatChannels.index",
    scope: "workspace",
    template: "/chat/channels",
    authenticatedSurface: true,
  }),
  route({
    name: "pay.index",
    scope: "workspace",
    template: "/pay",
    authenticatedSurface: true,
  }),
  route({
    name: "stays.index",
    scope: "workspace",
    template: "/stays",
    query: ["property_id"],
    authenticatedSurface: true,
  }),
  route({
    name: "instructions.index",
    scope: "workspace",
    template: "/instructions",
    authenticatedSurface: true,
  }),
  route({
    name: "properties.index",
    scope: "workspace",
    template: "/properties",
    authenticatedSurface: true,
  }),
  route({
    name: "documents.index",
    scope: "workspace",
    template: "/documents",
    authenticatedSurface: true,
  }),
  route({
    name: "assets.index",
    scope: "workspace",
    template: "/assets",
    query: ["category", "property_id"],
    authenticatedSurface: true,
  }),
  route({
    name: "inventory.index",
    scope: "workspace",
    template: "/inventory",
    authenticatedSurface: true,
  }),
  route({
    name: "assetTypes.index",
    scope: "workspace",
    template: "/asset_types",
    authenticatedSurface: true,
  }),
  route({
    name: "organizations.index",
    scope: "workspace",
    template: "/organizations",
    authenticatedSurface: true,
  }),
  route({
    name: "employees.index",
    scope: "workspace",
    template: "/employees",
    authenticatedSurface: true,
  }),
  route({
    name: "expenses.index",
    scope: "workspace",
    template: "/expenses",
    authenticatedSurface: true,
  }),
  route({
    name: "templates.index",
    scope: "workspace",
    template: "/templates",
    authenticatedSurface: true,
  }),
  route({
    name: "schedules.index",
    scope: "workspace",
    template: "/schedules",
    authenticatedSurface: true,
  }),
  route({
    name: "permissions.index",
    scope: "workspace",
    template: "/permissions",
    authenticatedSurface: true,
  }),
  route({
    name: "tokens.index",
    scope: "workspace",
    template: "/tokens",
    authenticatedSurface: true,
  }),
  route({
    name: "audit.index",
    scope: "workspace",
    template: "/audit",
    query: ["actor", "action", "entity", "since", "until"],
    authenticatedSurface: true,
  }),
  route({
    name: "portfolio.index",
    scope: "workspace",
    template: "/portfolio",
    authenticatedSurface: true,
  }),
  route({
    name: "billableHours.index",
    scope: "workspace",
    template: "/billable-hours",
    authenticatedSurface: true,
  }),
  route({
    name: "quotes.index",
    scope: "workspace",
    template: "/quotes",
    authenticatedSurface: true,
  }),
  route({
    name: "invoices.index",
    scope: "workspace",
    template: "/invoices",
    authenticatedSurface: true,
  }),
  route({
    name: "admin.dashboard",
    scope: "admin",
    template: "/admin/dashboard",
    authenticatedSurface: true,
  }),
  route({
    name: "admin.chatGateway",
    scope: "admin",
    template: "/admin/chat-gateway",
    authenticatedSurface: true,
  }),
  route({
    name: "admin.llm.index",
    scope: "admin",
    template: "/admin/llm",
    agentLinkable: false,
    authenticatedSurface: true,
  }),
  route({
    name: "admin.llm.graph",
    scope: "admin",
    template: "/admin/llm/graph",
    authenticatedSurface: true,
  }),
  route({
    name: "admin.llm.usage",
    scope: "admin",
    template: "/admin/llm/usage",
    authenticatedSurface: true,
  }),
  route({
    name: "admin.agentDocs",
    scope: "admin",
    template: "/admin/agent-docs",
    authenticatedSurface: true,
  }),
  route({
    name: "admin.usage",
    scope: "admin",
    template: "/admin/usage",
    authenticatedSurface: true,
  }),
  route({
    name: "admin.workspaces",
    scope: "admin",
    template: "/admin/workspaces",
    authenticatedSurface: true,
  }),
  route({
    name: "admin.signups",
    scope: "admin",
    template: "/admin/signups",
    authenticatedSurface: true,
  }),
  route({
    name: "admin.settings",
    scope: "admin",
    template: "/admin/settings",
    authenticatedSurface: true,
  }),
  route({
    name: "admin.admins",
    scope: "admin",
    template: "/admin/admins",
    authenticatedSurface: true,
  }),
  route({
    name: "admin.audit",
    scope: "admin",
    template: "/admin/audit",
    query: ["actor_kind", "actor_id", "action", "since", "until"],
    authenticatedSurface: true,
  }),
  route({
    name: "task.detail",
    scope: "workspace",
    template: "/task/:tid",
    params: ["tid"],
  }),
  route({
    name: "asset.detail",
    scope: "workspace",
    template: "/asset/:aid",
    params: ["aid"],
  }),
  route({
    name: "asset.scanToken",
    scope: "workspace",
    template: "/asset/scan/:token",
    params: ["token"],
    agentLinkable: false,
  }),
  route({
    name: "property.detail",
    scope: "workspace",
    template: "/property/:pid",
    params: ["pid"],
  }),
  route({
    name: "property.stays",
    scope: "workspace",
    template: "/property/:pid/stays",
    params: ["pid"],
  }),
  route({
    name: "property.instructions",
    scope: "workspace",
    template: "/property/:pid/instructions",
    params: ["pid"],
  }),
  route({
    name: "property.closures",
    scope: "workspace",
    template: "/property/:pid/closures",
    params: ["pid"],
  }),
  route({
    name: "property.assets",
    scope: "workspace",
    template: "/property/:pid/assets",
    params: ["pid"],
  }),
  route({
    name: "property.inventory",
    scope: "workspace",
    template: "/property/:pid/inventory",
    params: ["pid"],
  }),
  route({
    name: "employee.detail",
    scope: "workspace",
    template: "/employee/:eid",
    params: ["eid"],
  }),
  route({
    name: "employee.leaves",
    scope: "workspace",
    template: "/employee/:eid/leaves",
    params: ["eid"],
  }),
  route({
    name: "user.detail",
    scope: "workspace",
    template: "/user/:eid",
    params: ["eid"],
    agentLinkable: false,
  }),
  route({
    name: "user.leaves",
    scope: "workspace",
    template: "/user/:eid/leaves",
    params: ["eid"],
    agentLinkable: false,
  }),
  route({
    name: "instruction.detail",
    scope: "workspace",
    template: "/instructions/:iid",
    params: ["iid"],
  }),
] as const;

export type FrontendRoute = (typeof FRONTEND_ROUTES)[number];
export type FrontendRouteName = FrontendRoute["name"];

export function frontendRouteByName(name: string): FrontendRoute | undefined {
  return FRONTEND_ROUTES.find((entry) => entry.name === name);
}

export const FRONTEND_ROUTE_MANIFEST_JSON: FrontendRouteManifestJson = {
  version: FRONTEND_ROUTE_MANIFEST_VERSION,
  routes: FRONTEND_ROUTES.map(
    ({ authenticatedSurface: _authenticatedSurface, ...entry }) => entry,
  ),
};
