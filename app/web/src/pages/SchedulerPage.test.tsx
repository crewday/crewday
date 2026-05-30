import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { setAuthenticated } from "@/auth/authStore";
import { __resetAuthStoreForTests } from "@/auth/useAuth";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests, qk } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import type { GrantRole } from "@/types/auth";
import type { SchedulerCalendarPayload } from "@/types/api";
import SchedulerPage from "./SchedulerPage";
import appSource from "../App.tsx?raw";
import { jsonResponse } from "@/test/helpers";

const CALENDAR: SchedulerCalendarPayload = {
  window: { from: "2026-05-04", to: "2026-05-10" },
  rulesets: [{ id: "ruleset_housekeeping", workspace_id: "ws_1", name: "Housekeeping" }],
  slots: [
    {
      id: "slot_monday",
      schedule_ruleset_id: "ruleset_housekeeping",
      weekday: 0,
      starts_local: "08:00",
      ends_local: "12:00",
    },
  ],
  assignments: [
    {
      id: "assignment_alex",
      user_id: "user_alex",
      work_role_id: "role_cleaner",
      property_id: "prop_villa",
      schedule_ruleset_id: "ruleset_housekeeping",
    },
  ],
  tasks: [
    {
      id: "task_turnover",
      title: "Turnover clean",
      property_id: "prop_villa",
      user_id: "user_alex",
      scheduled_start: "2026-05-04T09:30:00Z",
      estimated_minutes: 90,
      priority: "normal",
      status: "pending",
    },
  ],
  users: [{ id: "user_alex", first_name: "Alex", display_name: "Alex Rivera" }],
  properties: [{ id: "prop_villa", name: "Villa Rosa", timezone: "Europe/Lisbon" }],
};

const EMPTY_CALENDAR: SchedulerCalendarPayload = {
  window: { from: "2026-05-04", to: "2026-05-10" },
  rulesets: [],
  slots: [],
  assignments: [],
  tasks: [],
  users: [],
  properties: [],
};

const observedTargets: Element[] = [];

class TestIntersectionObserver {
  private readonly callback: IntersectionObserverCallback;
  private readonly targets = new Set<Element>();

  constructor(callback: IntersectionObserverCallback) {
    this.callback = callback;
  }

  observe(target: Element): void {
    this.targets.add(target);
    observedTargets.push(target);
  }

  disconnect(): void {}

  unobserve(): void {}

  takeRecords(): IntersectionObserverEntry[] {
    return [];
  }

  trigger(target: Element, isIntersecting = true): void {
    if (!this.targets.has(target)) return;
    this.callback(
      [{ target, isIntersecting } as IntersectionObserverEntry],
      this as unknown as IntersectionObserver,
    );
  }
}

const testObservers: TestIntersectionObserver[] = [];

function installIntersectionObserver(): void {
  observedTargets.length = 0;
  testObservers.length = 0;
  Element.prototype.scrollIntoView = vi.fn();
  window.scrollBy = vi.fn();
  vi.stubGlobal(
    "IntersectionObserver",
    class extends TestIntersectionObserver {
      constructor(callback: IntersectionObserverCallback) {
        super(callback);
        testObservers.push(this);
      }
    },
  );
}

function triggerIntersect(selector: string, isIntersecting = true): void {
  const target = observedTargets.find((item) => item.matches(selector));
  if (!target) throw new Error(`No observed target matched ${selector}`);
  testObservers.forEach((observer) => observer.trigger(target, isIntersecting));
}

function startOfIsoWeekIso(d: Date): string {
  const out = new Date(d);
  out.setHours(0, 0, 0, 0);
  const iso = (out.getDay() + 6) % 7;
  out.setDate(out.getDate() - iso);
  const y = out.getFullYear();
  const m = String(out.getMonth() + 1).padStart(2, "0");
  const day = String(out.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function addDaysIso(iso: string, days: number): string {
  const [y, m, d] = iso.split("-").map(Number);
  const date = new Date(y!, (m ?? 1) - 1, d ?? 1);
  date.setDate(date.getDate() + days);
  const nextY = date.getFullYear();
  const nextM = String(date.getMonth() + 1).padStart(2, "0");
  const nextD = String(date.getDate()).padStart(2, "0");
  return `${nextY}-${nextM}-${nextD}`;
}

function isoDate(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function authenticate(grantRole: GrantRole): void {
  setAuthenticated({
    user_id: "usr_test",
    display_name: "Test User",
    email: "test@example.com",
    current_workspace_id: "ws_1",
    is_deployment_admin: false,
    available_workspaces: [
      {
        workspace_id: "acme",
        workspace: {
          id: "ws_1",
          name: "Acme",
          timezone: "UTC",
          default_currency: "USD",
          default_country: "US",
          default_locale: "en",
        },
        grant_role: grantRole,
        binding_org_id: grantRole === "client" ? "org_client" : null,
        source: "workspace_grant",
      },
    ],
  });
}

function installFetch(payload: SchedulerCalendarPayload = CALENDAR) {
  const calls: string[] = [];
  const original = globalThis.fetch;
  const spy = vi.fn(async (url: string | URL | Request) => {
    const resolved = typeof url === "string" ? url : url.toString();
    calls.push(resolved);
    const parsed = new URL(resolved, "http://crewday.local");
    const from = parsed.searchParams.get("from") ?? payload.window.from;
    const to = parsed.searchParams.get("to") ?? payload.window.to;
    return jsonResponse({
      ...payload,
      window: { from, to },
      tasks: payload.tasks.map((task) => ({
        ...task,
        scheduled_start: `${from}T09:30:00Z`,
      })),
    });
  });
  (globalThis as { fetch: typeof fetch }).fetch = spy as unknown as typeof fetch;
  return {
    calls,
    restore: () => {
      (globalThis as { fetch: typeof fetch }).fetch = original;
    },
  };
}

function Harness() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <WorkspaceProvider>
        <MemoryRouter initialEntries={["/scheduler"]}>
          <SchedulerPage />
        </MemoryRouter>
      </WorkspaceProvider>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  authenticate("manager");
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.spyOn(preferences, "readWorkspaceCookie").mockReturnValue("acme");
  installIntersectionObserver();
});

afterEach(() => {
  cleanup();
  __resetAuthStoreForTests();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("<SchedulerPage>", () => {
  it("wires the shared scheduler route inside the workspace tree", () => {
    expect(appSource).toContain('<Route path="scheduler" element={<SchedulerPage />} />');
    expect(appSource).toContain('<Route path="/w/:slug" element={<WorkspaceRouteRoot />}>');
  });

  it("loads the production calendar feed and renders the promoted grid", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      expect(await screen.findByText("Alex Rivera")).toBeInTheDocument();
      expect(screen.getByText("08:00–12:00")).toBeInTheDocument();
      expect(screen.getByText("Turnover clean")).toBeInTheDocument();
      expect(screen.getByText("Villa Rosa")).toBeInTheDocument();
      expect(fake.calls).toHaveLength(1);
      const currentWeek = startOfIsoWeekIso(new Date());
      expect(fake.calls[0]).toBe(
        `/w/acme/api/v1/scheduler/calendar?from=${currentWeek}&to=${addDaysIso(currentWeek, 6)}`,
      );
      expect(fake.calls[0]).not.toContain("from_=");
    } finally {
      fake.restore();
    }
  });

  it("renders scheduler guidance without visible spec markers or implementation terms", async () => {
    const fake = installFetch({ ...CALENDAR, tasks: [] });
    try {
      render(<Harness />);

      expect(
        await screen.findByText("Who is booked where, with scheduled shifts and assigned tasks."),
      ).toBeInTheDocument();
      expect(await screen.findAllByText("No task")).toHaveLength(2);

      const visibleText = document.body.textContent ?? "";
      expect(visibleText).toContain(
        "No task markers show scheduled shifts that do not have assigned work yet.",
      );
      expect(visibleText).not.toContain("§");
      expect(visibleText).not.toMatch(/\bmaterialised\b/i);
      expect(visibleText).not.toMatch(/\brota gap\b/i);
      expect(visibleText).not.toMatch(/\bruleset\b/i);
    } finally {
      fake.restore();
    }
  });

  it("does not repeat a scheduler row name when first and display names match", async () => {
    const fake = installFetch({
      ...CALENDAR,
      users: [
        { id: "user_alex", first_name: "Vincent", display_name: "  Vincent  " },
        { id: "user_me", first_name: "me", display_name: "  Me  " },
      ],
    });
    try {
      render(<Harness />);

      expect(await screen.findByText("Vincent")).toBeInTheDocument();
      expect(screen.getByText("me")).toBeInTheDocument();
      expect(screen.getAllByText("Vincent")).toHaveLength(1);
      expect(screen.queryByText("Me")).toBeNull();
      expect(document.querySelector(".scheduler-row__sub")).toBeNull();
    } finally {
      fake.restore();
    }
  });

  it("keeps richer display names visible for manager scheduler rows", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      expect(await screen.findByText("Alex")).toBeInTheDocument();
      expect(screen.getByText("Alex Rivera")).toBeInTheDocument();
      expect(document.querySelector(".scheduler-row__sub")?.textContent).toBe("Alex Rivera");
    } finally {
      fake.restore();
    }
  });

  it("fetches and appends the next scheduler week from the bottom sentinel", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      await screen.findByText("Alex Rivera");
      const currentWeek = startOfIsoWeekIso(new Date());
      await act(async () => {
        triggerIntersect(".schedule__sentinel--bot");
      });

      await waitFor(() => expect(fake.calls).toHaveLength(2));
      const nextWeek = addDaysIso(currentWeek, 7);
      expect(fake.calls[1]).toBe(
        `/w/acme/api/v1/scheduler/calendar?from=${nextWeek}&to=${addDaysIso(nextWeek, 6)}`,
      );
    } finally {
      fake.restore();
    }
  });

  it("fetches and prepends the previous scheduler week from the top sentinel", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      await screen.findByText("Alex Rivera");
      const currentWeek = startOfIsoWeekIso(new Date());
      await act(async () => {
        triggerIntersect(".schedule__sentinel--top");
      });

      await waitFor(() => expect(fake.calls).toHaveLength(2));
      const previousWeek = addDaysIso(currentWeek, -7);
      expect(fake.calls[1]).toBe(
        `/w/acme/api/v1/scheduler/calendar?from=${previousWeek}&to=${addDaysIso(previousWeek, 6)}`,
      );
    } finally {
      fake.restore();
    }
  });

  it("preserves the viewport when prepending after the initial settle window", async () => {
    const fake = installFetch();
    let scrollHeight = 1_000;
    const scrollHeightSpy = vi
      .spyOn(document.documentElement, "scrollHeight", "get")
      .mockImplementation(() => scrollHeight);
    try {
      render(<Harness />);

      await screen.findByText("Alex Rivera");
      await act(async () => {
        await new Promise((resolve) => window.setTimeout(resolve, 400));
      });

      scrollHeight = 1_000;
      await act(async () => {
        triggerIntersect(".schedule__sentinel--top");
      });
      scrollHeight = 1_420;

      await waitFor(() =>
        expect(window.scrollBy).toHaveBeenCalledWith({
          top: 420,
          behavior: "instant",
        }),
      );
    } finally {
      scrollHeightSpy.mockRestore();
      fake.restore();
    }
  });

  it("jumps back to today's loaded cell from the Today affordance", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      await screen.findByText("Alex Rivera");
      const todayIso = isoDate(new Date());
      await act(async () => {
        triggerIntersect(`[data-scheduler-iso="${todayIso}"]`, false);
      });

      const jump = await screen.findByRole("button", { name: "Jump to today" });
      fireEvent.click(jump);

      const todayCell = document.querySelector(`[data-scheduler-iso="${todayIso}"]`);
      expect(todayCell?.scrollIntoView).toHaveBeenCalledWith({
        block: "start",
        behavior: "smooth",
      });
    } finally {
      fake.restore();
    }
  });

  it("refetches every loaded scheduler page through the SSE scheduler prefix", async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const fake = installFetch();
    try {
      render(
        <QueryClientProvider client={queryClient}>
          <WorkspaceProvider>
            <MemoryRouter initialEntries={["/scheduler"]}>
              <SchedulerPage />
            </MemoryRouter>
          </WorkspaceProvider>
        </QueryClientProvider>,
      );

      await screen.findByText("Alex Rivera");
      await act(async () => {
        triggerIntersect(".schedule__sentinel--bot");
      });
      await waitFor(() => expect(fake.calls).toHaveLength(2));
      await act(async () => {
        triggerIntersect(".schedule__sentinel--top");
      });
      await waitFor(() => expect(fake.calls).toHaveLength(3));

      const callsBeforeInvalidation = fake.calls.length;
      await act(async () => {
        await queryClient.invalidateQueries({ queryKey: qk.schedulerCalendarPrefix() });
      });

      await waitFor(() => expect(fake.calls.length).toBe(callsBeforeInvalidation + 3));
      const refetches = fake.calls.slice(callsBeforeInvalidation);
      const currentWeek = startOfIsoWeekIso(new Date());
      const expectedWeeks = [
        addDaysIso(currentWeek, -7),
        currentWeek,
        addDaysIso(currentWeek, 7),
      ];
      expect(refetches).toEqual(
        expectedWeeks.map(
          (from) => `/w/acme/api/v1/scheduler/calendar?from=${from}&to=${addDaysIso(from, 6)}`,
        ),
      );
    } finally {
      fake.restore();
      queryClient.clear();
    }
  });

  it("removes visible previous and next week navigation buttons", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      await screen.findByText("Alex Rivera");
      expect(screen.queryByRole("button", { name: /previous/i })).toBeNull();
      expect(screen.queryByRole("button", { name: /next/i })).toBeNull();
      expect(document.querySelector(".scheduler-weeknav")).toBeNull();
    } finally {
      fake.restore();
    }
  });

  it("keeps client views to first names while still rendering scoped rota data", async () => {
    authenticate("client");
    const fake = installFetch({
      ...CALENDAR,
      users: [{ id: "user_alex", first_name: "Alex", display_name: "  Alex Rivera  " }],
    });
    try {
      render(<Harness />);

      expect(await screen.findByText("Alex")).toBeInTheDocument();
      expect(screen.queryByText("Alex Rivera")).toBeNull();
      expect(screen.getByText("08:00–12:00")).toBeInTheDocument();
      expect(screen.queryByText("gap")).toBeNull();
    } finally {
      fake.restore();
    }
  });

  it("does not fall back to client-visible display names when first name is absent", async () => {
    authenticate("client");
    const fake = installFetch({
      ...CALENDAR,
      users: [{ id: "user_alex", first_name: "", display_name: "Alex Rivera" }],
    });
    try {
      render(<Harness />);

      expect(await screen.findByText(",")).toBeInTheDocument();
      expect(screen.queryByText("Alex Rivera")).toBeNull();
      expect(document.querySelector(".scheduler-row__sub")).toBeNull();
    } finally {
      fake.restore();
    }
  });

  it("renders the current actor row when an employee or manager week is empty", async () => {
    const fake = installFetch(EMPTY_CALENDAR);
    try {
      render(<Harness />);

      expect(await screen.findByText("Test User")).toBeInTheDocument();
      expect(screen.queryByText("No schedule data yet")).toBeNull();
    } finally {
      fake.restore();
    }
  });

  it("does not add the authenticated client to an empty scheduler feed", async () => {
    authenticate("client");
    const fake = installFetch(EMPTY_CALENDAR);
    try {
      render(<Harness />);

      expect(await screen.findByText("No schedule data yet")).toBeInTheDocument();
      const visibleText = document.body.textContent ?? "";
      expect(visibleText).not.toContain("§");
      expect(visibleText).not.toMatch(/\bmaterialised\b/i);
      expect(visibleText).not.toMatch(/\brota gap\b/i);
      expect(visibleText).not.toMatch(/\bruleset\b/i);
      expect(screen.queryByText("Test User")).toBeNull();
      expect(screen.queryByText("test@example.com")).toBeNull();
    } finally {
      fake.restore();
    }
  });
});
