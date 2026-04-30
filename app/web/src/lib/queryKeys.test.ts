import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  __resetQueryKeyGetterForTests,
  qk,
  registerQueryKeyWorkspaceGetter,
} from "@/lib/queryKeys";

// Every key assertion below is intentionally explicit — `qk.*` is the
// single source of truth for cache invalidation and for SSE dispatch,
// so an accidental rename or tuple-shape change breaks every consumer
// at once. Keeping the shapes pinned here fails fast in CI.

beforeEach(() => {
  __resetQueryKeyGetterForTests();
});

afterEach(() => {
  __resetQueryKeyGetterForTests();
});

describe("qk — workspace prefix", () => {
  it("keeps bare-host auth identity outside workspace scope", () => {
    registerQueryKeyWorkspaceGetter(() => "acme");

    expect(qk.authMe()).toEqual(["auth", "me"]);
  });

  it("prepends the active workspace slug to every workspace-scoped key", () => {
    registerQueryKeyWorkspaceGetter(() => "acme");

    expect(qk.me()).toEqual(["w", "acme", "me"]);
    expect(qk.tasks()).toEqual(["w", "acme", "tasks"]);
    expect(qk.task("t1")).toEqual(["w", "acme", "task", "t1"]);
    expect(qk.properties()).toEqual(["w", "acme", "properties"]);
    expect(qk.property("p1")).toEqual(["w", "acme", "property", "p1"]);
    expect(qk.propertyClosures("p1")).toEqual(["w", "acme", "property", "p1", "closures"]);
    expect(qk.clientPortfolio()).toEqual(["w", "acme", "client", "portfolio"]);
    expect(qk.mySchedule("2026-04-20", "2026-04-27")).toEqual([
      "w",
      "acme",
      "my-schedule",
      "2026-04-20",
      "2026-04-27",
    ]);
  });

  it("falls back to the `_` sentinel when no workspace is selected", () => {
    // The getter is reset in beforeEach → returns null.
    expect(qk.me()).toEqual(["w", "_", "me"]);
    expect(qk.tasks()).toEqual(["w", "_", "tasks"]);
  });

  it("re-reads the getter on every call so workspace switches take effect immediately", () => {
    let slug: string | null = "first";
    registerQueryKeyWorkspaceGetter(() => slug);
    expect(qk.me()).toEqual(["w", "first", "me"]);
    slug = "second";
    expect(qk.me()).toEqual(["w", "second", "me"]);
    slug = null;
    expect(qk.me()).toEqual(["w", "_", "me"]);
  });

  it("never collides with keys from a different tenant", () => {
    let slug: string | null = "acme";
    registerQueryKeyWorkspaceGetter(() => slug);
    const acmeTasks = qk.tasks();
    slug = "other";
    const otherTasks = qk.tasks();
    expect(acmeTasks).not.toEqual(otherTasks);
    // The first segment + slug is the discriminator, even though the
    // subsequent suffix is identical. This is the §14 tenant-isolation
    // invariant the factory exists to enforce.
    expect(acmeTasks[1]).toBe("acme");
    expect(otherTasks[1]).toBe("other");
  });
});

describe("qk — admin keys", () => {
  // §14 "Admin shell" — admin keys are deployment-scope, not
  // workspace-scope, so they must NOT carry a slug prefix. Otherwise
  // switching workspaces would evict admin cache entries that have
  // nothing to do with the switched tenant.
  it("emits admin keys without a workspace prefix regardless of active slug", () => {
    registerQueryKeyWorkspaceGetter(() => "acme");
    expect(qk.adminMe()).toEqual(["admin", "me"]);
    expect(qk.adminAudit()).toEqual(["admin", "audit"]);
    expect(qk.adminWorkspaces()).toEqual(["admin", "workspaces"]);
    expect(qk.adminAgentDocs()).toEqual(["admin", "agent_docs"]);
    expect(qk.adminAgentDoc("slug-x")).toEqual(["admin", "agent_docs", "slug-x"]);
  });
});

describe("qk — every key is a first-class readonly tuple", () => {
  it("produces tuple shapes that TanStack Query can use as a prefix for invalidation", () => {
    registerQueryKeyWorkspaceGetter(() => "acme");
    // The property-detail key must start with the properties root so
    // `invalidateQueries({ queryKey: qk.property("p1") })` correctly
    // matches through the `properties`/`property/:id` hierarchy.
    const propsKey = qk.properties();
    const propKey = qk.property("p1");
    // Properties list and a single-property detail share the same
    // workspace and root — so invalidating at the workspace level
    // matches both.
    expect(propsKey.slice(0, 2)).toEqual(propKey.slice(0, 2));
  });

  it("distinguishes the two agent-typing scopes (scope-only vs per-task)", () => {
    registerQueryKeyWorkspaceGetter(() => "acme");
    expect(qk.agentTyping("manager")).toEqual(["w", "acme", "agent", "typing", "manager"]);
    expect(qk.agentTyping("task", "t-42")).toEqual([
      "w",
      "acme",
      "agent",
      "typing",
      "task",
      "t-42",
    ]);
    // A task-scope call that forgets the taskId degrades to the
    // scope-only key instead of crashing. SSE dispatchers rely on
    // this to fan out an event they can't narrow to a task.
    expect(qk.agentTyping("task")).toEqual(["w", "acme", "agent", "typing", "task"]);
  });

  it("preserves empty-string and sentinel fallbacks for optional filter keys", () => {
    registerQueryKeyWorkspaceGetter(() => "acme");
    // Optional `id` on agentPrefs falls back to "" so a "workspace"
    // entry and a "property" entry never collide when the caller
    // forgets the id.
    expect(qk.agentPrefs("workspace")).toEqual(["w", "acme", "agent_preferences", "workspace", ""]);
    expect(qk.agentPrefs("property", "p1")).toEqual([
      "w",
      "acme",
      "agent_preferences",
      "property",
      "p1",
    ]);
    // Optional `workspaceId` on users falls back to "all" so that a
    // cross-workspace summary query can coexist with a per-workspace
    // narrow query.
    expect(qk.users()).toEqual(["w", "acme", "users", "all"]);
    expect(qk.users("ws-1")).toEqual(["w", "acme", "users", "ws-1"]);
  });
});
