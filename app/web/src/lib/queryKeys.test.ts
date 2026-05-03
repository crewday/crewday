import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { QueryClient } from "@tanstack/react-query";
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
    expect(qk.clientQuotes()).toEqual(["w", "acme", "client", "quotes"]);
    expect(qk.mySchedulePages("2026-04-20")).toEqual([
      "w",
      "acme",
      "my-schedule",
      "infinite",
      "2026-04-20",
    ]);
    expect(qk.mySchedulePrefix()).toEqual(["w", "acme", "my-schedule"]);
    expect(qk.schedulerCalendarPrefix()).toEqual([
      "w",
      "acme",
      "scheduler-calendar",
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

  it("adminAudit() returns the bare prefix when no filter is supplied or the filter is empty", () => {
    expect(qk.adminAudit()).toEqual(["admin", "audit"]);
    expect(qk.adminAudit({})).toEqual(["admin", "audit"]);
    expect(qk.adminAudit({ actor_id: "" })).toEqual(["admin", "audit"]);
  });

  it("adminAudit() appends the filter object so each combination caches independently", () => {
    expect(qk.adminAudit({ actor_id: "u-elodie" })).toEqual([
      "admin",
      "audit",
      { actor_id: "u-elodie" },
    ]);
    expect(qk.adminAudit({ action: "x", since: "2026-05-03T00:00:00Z" })).toEqual([
      "admin",
      "audit",
      { action: "x", since: "2026-05-03T00:00:00Z" },
    ]);
  });

  it("adminAudit() filtered variants invalidate when the bare prefix is invalidated (SSE path)", async () => {
    const qc = new QueryClient();
    const bare = qk.adminAudit();
    const filteredA = qk.adminAudit({ action: "deployment.budget.updated" });
    const filteredB = qk.adminAudit({
      action: "deployment.budget.adjusted",
      actor_id: "agent-admin",
    });
    qc.setQueryData(bare, { marker: "bare" });
    qc.setQueryData(filteredA, { marker: "a" });
    qc.setQueryData(filteredB, { marker: "b" });

    // The SSE handler for `admin.audit.appended` calls
    // `invalidate(qc, qk.adminAudit())` — i.e. the bare prefix. Every
    // filtered variant must drop too, otherwise a tab with an active
    // filter would silently miss appended rows until a manual refetch.
    await qc.invalidateQueries({ queryKey: qk.adminAudit() });

    expect(qc.getQueryState(bare)?.isInvalidated).toBe(true);
    expect(qc.getQueryState(filteredA)?.isInvalidated).toBe(true);
    expect(qc.getQueryState(filteredB)?.isInvalidated).toBe(true);
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

// cd-z1vj regression guard. The whole point of `mySchedulePrefix()` /
// `schedulerCalendarPrefix()` is that "the call happened" is not the
// same as "the cache was invalidated" — TanStack v5's prefix match
// starts at index 0 of the cached key, so a bare `["my-schedule"]`
// shortcut would silently miss the workspace-scoped cache. The
// existing sse.test.ts assertions only check call shape; they would
// not catch a future revert to bare keys. These tests use a real
// `QueryClient` so a regression flips them red.
describe("qk — workspace-prefix invalidation actually matches the cache", () => {
  beforeEach(() => {
    __resetQueryKeyGetterForTests();
    registerQueryKeyWorkspaceGetter(() => "acme");
  });

  it("mySchedulePrefix() invalidates this tenant's pages and not another tenant's", async () => {
    const qc = new QueryClient();
    const acmeKey = qk.mySchedulePages("2026-04-20");
    const otherKey = ["w", "other", "my-schedule", "infinite", "2026-04-20"] as const;
    qc.setQueryData(acmeKey, { marker: "acme" });
    qc.setQueryData(otherKey, { marker: "other" });

    await qc.invalidateQueries({ queryKey: qk.mySchedulePrefix() });

    expect(qc.getQueryState(acmeKey)?.isInvalidated).toBe(true);
    expect(qc.getQueryState(otherKey)?.isInvalidated).toBe(false);
  });

  it("schedulerCalendarPrefix() invalidates this tenant's calendar and not another tenant's", async () => {
    const qc = new QueryClient();
    const acmeKey = qk.schedulerCalendar("2026-04-20", "2026-04-26");
    const otherKey = ["w", "other", "scheduler-calendar", "2026-04-20", "2026-04-26"] as const;
    qc.setQueryData(acmeKey, { marker: "acme" });
    qc.setQueryData(otherKey, { marker: "other" });

    await qc.invalidateQueries({ queryKey: qk.schedulerCalendarPrefix() });

    expect(qc.getQueryState(acmeKey)?.isInvalidated).toBe(true);
    expect(qc.getQueryState(otherKey)?.isInvalidated).toBe(false);
  });

  it("a bare bare-key shortcut would NOT match the workspace-scoped cache (negative control)", async () => {
    // The exact failure mode cd-z1vj fixed. Pinned here so a future
    // contributor who shortens the helper to `["my-schedule"]` sees
    // it fail loudly rather than silently regress.
    const qc = new QueryClient();
    const acmeKey = qk.mySchedulePages("2026-04-20");
    qc.setQueryData(acmeKey, { marker: "acme" });

    await qc.invalidateQueries({ queryKey: ["my-schedule"] });

    expect(qc.getQueryState(acmeKey)?.isInvalidated).toBe(false);
  });
});
