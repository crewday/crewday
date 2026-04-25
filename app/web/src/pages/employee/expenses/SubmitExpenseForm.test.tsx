// Component-level coverage for the worker's submit form.
//
// The unit tests in `lib/expenses.test.ts` already pin
// `buildExpenseClaimCreatePayload` field-by-field; this file is the
// thin "the form actually wires its inputs into that helper" check.
// We mount the real component, stub `fetch` for `/me`, the active-
// engagement lookup, the property list, and the create POST, then
// assert on the body the form sent to `/api/v1/expenses`.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  __resetApiProvidersForTests,
  registerWorkspaceSlugGetter,
} from "@/lib/api";
import SubmitExpenseForm from "./SubmitExpenseForm";

interface ScriptedResponse {
  status?: number;
  body: unknown;
}

interface FetchCall {
  url: string;
  init: RequestInit;
}

/**
 * Wire a scripted `fetch` keyed by URL suffix. Mirrors the pattern
 * from `LoginPage.test.tsx` so each endpoint's queue is independent.
 */
function installFetch(
  scripted: Record<string, ScriptedResponse[]>,
): { calls: FetchCall[]; restore: () => void } {
  const calls: FetchCall[] = [];
  const original = globalThis.fetch;
  const queues: Record<string, ScriptedResponse[]> = {};
  for (const [k, v] of Object.entries(scripted)) queues[k] = [...v];
  const spy = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const resolved = typeof url === "string" ? url : url.toString();
    calls.push({ url: resolved, init: init ?? {} });
    const suffix = Object.keys(queues).find((s) => resolved.includes(s));
    if (!suffix) throw new Error(`Unscripted fetch: ${resolved}`);
    const next = queues[suffix]!.shift();
    if (!next) throw new Error(`No more responses for: ${resolved}`);
    const status = next.status ?? 200;
    const ok = status >= 200 && status < 300;
    const text = JSON.stringify(next.body);
    return {
      ok,
      status,
      statusText: ok ? "OK" : "Error",
      text: async () => text,
    } as unknown as Response;
  });
  (globalThis as { fetch: typeof fetch }).fetch = spy as unknown as typeof fetch;
  return {
    calls,
    restore: () => {
      (globalThis as { fetch: typeof fetch }).fetch = original;
    },
  };
}

function meBody(): unknown {
  return {
    role: "worker",
    theme: "system",
    agent_sidebar_collapsed: false,
    employee: {
      id: "emp-1",
      name: "Maya",
      roles: ["worker"],
      avatar_initials: "M",
      avatar_url: null,
    },
    manager_name: "Alex",
    today: "2026-04-25",
    now: "2026-04-25T09:00:00Z",
    user_id: "u-1",
    agent_approval_mode: "manual",
    current_workspace_id: "ws-1",
    available_workspaces: [],
    client_binding_org_ids: [],
    is_deployment_admin: false,
    is_deployment_owner: false,
  };
}

function engagementListBody(): unknown {
  return {
    data: [
      {
        id: "we-1",
        user_id: "u-1",
        workspace_id: "ws-1",
        engagement_kind: "direct",
        supplier_org_id: null,
        pay_destination_id: null,
        reimbursement_destination_id: null,
        started_on: "2025-01-01",
        archived_on: null,
        notes_md: "",
        created_at: "2025-01-01T00:00:00Z",
        updated_at: "2025-01-01T00:00:00Z",
      },
    ],
    next_cursor: null,
    has_more: false,
  };
}

function propertiesBody(): unknown {
  return [
    {
      id: "prop-1",
      name: "Sunset Villa",
      city: "Nice",
      timezone: "Europe/Paris",
      color: "moss",
      kind: "vacation",
      areas: [],
      evidence_policy: "inherit",
      country: "FR",
      locale: "fr-FR",
      settings_override: {},
      client_org_id: null,
      owner_user_id: null,
    },
  ];
}

function expenseCreatedBody(): unknown {
  return {
    id: "claim-1",
    workspace_id: "ws-1",
    work_engagement_id: "we-1",
    vendor: "Carrefour",
    purchased_at: "2026-04-20T00:00:00Z",
    currency: "EUR",
    total_amount_cents: 1234,
    category: "supplies",
    property_id: null,
    note_md: "",
    state: "draft",
    submitted_at: null,
    decided_by: null,
    decided_at: null,
    decision_note_md: null,
    created_at: "2026-04-25T09:00:00Z",
    deleted_at: null,
    attachments: [],
  };
}

function withQueryClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

beforeEach(() => {
  __resetApiProvidersForTests();
  registerWorkspaceSlugGetter(() => "acme");
  document.cookie = "crewday_csrf=; path=/; max-age=0";
});

afterEach(() => {
  cleanup();
  __resetApiProvidersForTests();
});

describe("SubmitExpenseForm", () => {
  it("posts the cd-t6y2 ExpenseClaimCreate body shape on submit", async () => {
    const env = installFetch({
      "/api/v1/me": [{ body: meBody() }],
      "/api/v1/work_engagements": [{ body: engagementListBody() }],
      "/api/v1/properties": [{ body: propertiesBody() }],
      "/api/v1/expenses": [{ body: expenseCreatedBody(), status: 201 }],
    });
    const onSubmitted = vi.fn();
    const onBack = vi.fn();

    const qc = withQueryClient();
    render(
      <QueryClientProvider client={qc}>
        <SubmitExpenseForm
          initialScan={null}
          onSubmitted={onSubmitted}
          onBack={onBack}
        />
      </QueryClientProvider>,
    );

    // Wait for `/me` and the engagement lookup to land — once the
    // submit button is enabled the work_engagement_id is wired in.
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /submit expense/i }),
      ).not.toBeDisabled();
    });
    // Wait for the property list to populate the select.
    await waitFor(() => {
      expect(screen.getByRole("option", { name: "Sunset Villa" })).toBeInTheDocument();
    });

    // Fill the form. Manual-entry path → defaults to today + EUR +
    // category="other"; the test changes the fields whose mapping
    // matters most for the wire shape.
    const vendor = screen.getByPlaceholderText("e.g. Carrefour") as HTMLInputElement;
    fireEvent.change(vendor, { target: { value: "Carrefour" } });
    const amount = screen.getByPlaceholderText("0.00") as HTMLInputElement;
    fireEvent.change(amount, { target: { value: "12.34" } });
    const purchasedOn = screen
      .getByDisplayValue(todayDateInput()) as HTMLInputElement;
    fireEvent.change(purchasedOn, { target: { value: "2026-04-20" } });
    // Pick a category radio so the wire body lands on "supplies"
    // rather than the default.
    fireEvent.click(screen.getByLabelText("Supplies"));
    // Pick a property to exercise the property_id branch.
    const propSelect = screen.getByDisplayValue(
      "— No property —",
    ) as HTMLSelectElement;
    fireEvent.change(propSelect, { target: { value: "prop-1" } });
    // Type a note so `note_md` shows up on the wire.
    const note = screen.getByPlaceholderText("What it was for") as HTMLTextAreaElement;
    fireEvent.change(note, { target: { value: "Cleaning supplies" } });

    fireEvent.submit(vendor.closest("form") as HTMLFormElement);

    await waitFor(() => {
      expect(
        env.calls.find(
          (c) => c.init.method === "POST" && c.url.endsWith("/api/v1/expenses"),
        ),
      ).toBeDefined();
    });

    // Locate the POST call.
    const postCall = env.calls.find(
      (c) => c.init.method === "POST" && c.url.endsWith("/api/v1/expenses"),
    );
    expect(postCall, "POST /api/v1/expenses was not made").toBeDefined();
    const sent = JSON.parse(postCall!.init.body as string) as Record<string, unknown>;

    // Fields the cd-t6y2 ExpenseClaimCreate schema requires.
    expect(sent.work_engagement_id).toBe("we-1");
    expect(sent.vendor).toBe("Carrefour");
    expect(sent.currency).toBe("EUR");
    expect(sent.category).toBe("supplies");
    expect(sent.total_amount_cents).toBe(1234);
    expect(sent.property_id).toBe("prop-1");
    expect(sent.note_md).toBe("Cleaning supplies");
    // `purchased_at` is a `Z`-suffixed ISO string anchored on the
    // local-noon of 2026-04-20 (see `isoFromDateInput`'s rationale).
    expect(typeof sent.purchased_at).toBe("string");
    expect(sent.purchased_at as string).toMatch(/Z$/);
    const round = new Date(sent.purchased_at as string);
    expect(round.getFullYear()).toBe(2026);
    expect(round.getMonth()).toBe(3); // April (0-indexed)
    expect(round.getDate()).toBe(20);
    expect(round.getHours()).toBe(12);
    // Per-field type pins so a drift in any value's wire shape
    // surfaces here before the server returns 422.
    expect(typeof sent.work_engagement_id).toBe("string");
    expect(typeof sent.vendor).toBe("string");
    expect(typeof sent.currency).toBe("string");
    expect(typeof sent.category).toBe("string");
    expect(typeof sent.total_amount_cents).toBe("number");
    expect(typeof sent.property_id).toBe("string");
    expect(typeof sent.note_md).toBe("string");
    // Lock the exact key set — the server's `extra="forbid"` would
    // 422 on any drift, but pinning it here surfaces the regression
    // in the unit tier before the integration suite even runs.
    expect(Object.keys(sent).sort()).toEqual([
      "category",
      "currency",
      "note_md",
      "property_id",
      "purchased_at",
      "total_amount_cents",
      "vendor",
      "work_engagement_id",
    ]);

    // None of the legacy mock-era fields are sent.
    expect(sent).not.toHaveProperty("merchant");
    expect(sent).not.toHaveProperty("amount");
    expect(sent).not.toHaveProperty("note");
    expect(sent).not.toHaveProperty("ocr_confidence");

    // The form fired the success callback once the mutation
    // resolved. `waitFor` lets the onSuccess micro-task land
    // without the test pinning a specific microtask depth.
    await waitFor(() => {
      expect(onSubmitted).toHaveBeenCalledTimes(1);
    });
  });

  it("disables submit and surfaces a message when the user has no active engagement", async () => {
    installFetch({
      "/api/v1/me": [{ body: meBody() }],
      "/api/v1/work_engagements": [
        { body: { data: [], next_cursor: null, has_more: false } },
      ],
      "/api/v1/properties": [{ body: propertiesBody() }],
    });
    const qc = withQueryClient();
    render(
      <QueryClientProvider client={qc}>
        <SubmitExpenseForm
          initialScan={null}
          onSubmitted={vi.fn()}
          onBack={vi.fn()}
        />
      </QueryClientProvider>,
    );

    await waitFor(() => {
      expect(
        screen.getByText(/no active work engagement/i),
      ).toBeInTheDocument();
    });
    const submitBtn = screen.getByRole("button", { name: /submit expense/i });
    expect(submitBtn).toBeDisabled();
  });

  it("omits property_id from the wire body when no property is selected", async () => {
    const env = installFetch({
      "/api/v1/me": [{ body: meBody() }],
      "/api/v1/work_engagements": [{ body: engagementListBody() }],
      "/api/v1/properties": [{ body: propertiesBody() }],
      "/api/v1/expenses": [{ body: expenseCreatedBody(), status: 201 }],
    });
    const qc = withQueryClient();
    render(
      <QueryClientProvider client={qc}>
        <SubmitExpenseForm
          initialScan={null}
          onSubmitted={vi.fn()}
          onBack={vi.fn()}
        />
      </QueryClientProvider>,
    );
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /submit expense/i }),
      ).not.toBeDisabled();
    });

    fireEvent.change(screen.getByPlaceholderText("e.g. Carrefour"), {
      target: { value: "Bricorama" },
    });
    fireEvent.change(screen.getByPlaceholderText("0.00"), {
      target: { value: "5.00" },
    });
    const form = screen
      .getByPlaceholderText("e.g. Carrefour")
      .closest("form") as HTMLFormElement;
    fireEvent.submit(form);

    await waitFor(() => {
      expect(
        env.calls.find(
          (c) => c.init.method === "POST" && c.url.endsWith("/api/v1/expenses"),
        ),
      ).toBeDefined();
    });
    const postCall = env.calls.find(
      (c) => c.init.method === "POST" && c.url.endsWith("/api/v1/expenses"),
    );
    expect(postCall).toBeDefined();
    const sent = JSON.parse(postCall!.init.body as string) as Record<string, unknown>;
    // The optional pin is omitted, not present-with-null — the
    // server treats both as "no property" but the wire shape says
    // omit, so we lock that in here.
    expect("property_id" in sent).toBe(false);
  });
});

// Local helper duplicating `todayDateInput` from the form's lib so
// the test asserts against the same calendar value the component
// renders today, without coupling the test file to the helper's
// internals (a regression in the export shape would be caught by
// the lib's own `scanDerivation.test.ts`).
function todayDateInput(): string {
  const now = new Date();
  const y = now.getFullYear();
  const m = String(now.getMonth() + 1).padStart(2, "0");
  const d = String(now.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}
