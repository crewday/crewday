import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import WhoCanDoThis from "./WhoCanDoThis";

function renderResolver() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const fetchSpy = vi.fn(async (_url: string | URL | Request) => ({
    ok: true,
    status: 200,
    statusText: "OK",
    text: async () =>
      JSON.stringify({
        effect: "allow",
        source_layer: "default",
        source_rule_id: null,
        matched_groups: [],
      }),
  } as Response));
  (globalThis as { fetch: typeof fetch }).fetch = fetchSpy as unknown as typeof fetch;

  render(
    <QueryClientProvider client={qc}>
      <WhoCanDoThis
        users={[
          { id: "user_alice", display_name: "Alice", email: "alice@example.com" },
          { id: "user_bob", display_name: "Bob", email: "bob@example.com" },
        ]}
        actions={[
          {
            key: "employees.read",
            valid_scope_kinds: ["workspace"],
            default_allow: ["owners"],
            root_only: false,
            root_protected_deny: false,
          },
          {
            key: "tasks.assign_other",
            valid_scope_kinds: ["workspace", "property"],
            default_allow: ["managers"],
            root_only: false,
            root_protected_deny: false,
          },
        ]}
        scopeKind="workspace"
        scopeId="ws_1"
      />
    </QueryClientProvider>,
  );

  return fetchSpy;
}

describe("WhoCanDoThis", () => {
  it("uses searchable user and action controls while preserving selected ids", async () => {
    const fetchSpy = renderResolver();

    const user = screen.getByRole("combobox", { name: /^User/ });
    const action = screen.getByRole("combobox", { name: /^Action/ });
    expect(user).toHaveValue("Alice");
    expect(action).toHaveValue("employees.read");

    fireEvent.focus(user);
    fireEvent.change(user, { target: { value: "bob" } });
    fireEvent.mouseDown(screen.getByRole("option", { name: /Bob/ }));

    fireEvent.focus(action);
    fireEvent.change(action, { target: { value: "assign" } });
    fireEvent.mouseDown(screen.getByRole("option", { name: /tasks.assign_other/ }));

    await waitFor(() => {
      expect(
        fetchSpy.mock.calls.some(([url]) => {
          const parsed = new URL(String(url), "http://crewday.test");
          return (
            parsed.searchParams.get("user_id") === "user_bob" &&
            parsed.searchParams.get("action_key") === "tasks.assign_other"
          );
        }),
      ).toBe(true);
    });
  });
});
