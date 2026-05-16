import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import InlineErrorAlert from "./InlineErrorAlert";
import type { DisplayError } from "@/lib/displayError";

const displayError: DisplayError = {
  id: "err_inline_123",
  message: "Could not save assignment.",
  status: 409,
  type: "conflict",
  title: "Conflict",
  machineCode: "capability_inheritance_cycle",
  instance: "/admin/api/v1/llm/inheritance/chat.manager",
  fieldErrors: [{
    loc: ["body", "inherits_from"],
    msg: "Parent would create a cycle",
    type: "value_error",
  }],
  requestId: "req_inline_456",
  raw: null,
  details: [{
    label: "Approval request id",
    message: "apr_789",
    path: "approval_request_id",
    type: "extension",
  }],
};

beforeEach(() => {
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText: vi.fn().mockResolvedValue(undefined) },
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("InlineErrorAlert", () => {
  it("renders a collapsed inline alert with only the user-facing copy visible", () => {
    render(<InlineErrorAlert error={displayError} />);

    const alert = screen.getByRole("alert");
    expect(within(alert).getByText("Could not save assignment.")).toBeInTheDocument();
    expect(within(alert).getByRole("button", { name: "Show error details" })).toBeInTheDocument();
    expect(within(alert).queryByText("err_inline_123")).not.toBeInTheDocument();
    expect(within(alert).queryByText("Conflict")).not.toBeInTheDocument();
    expect(within(alert).queryByText("Approval request id: apr_789")).not.toBeInTheDocument();
  });

  it("expands and collapses details while keeping the toggle focused", () => {
    render(
      <InlineErrorAlert
        error={{
          ...displayError,
          details: [
            ...displayError.details,
            {
              label: "Field error",
              message: "extension value that shares a promoted label",
              path: "field_error",
              type: "extension",
            },
          ],
        }}
      />,
    );

    const toggle = screen.getByRole("button", { name: "Show error details" });
    toggle.focus();
    fireEvent.click(toggle);

    const alert = screen.getByRole("alert");
    expect(toggle).toHaveFocus();
    expect(within(alert).getByText("err_inline_123")).toBeInTheDocument();
    expect(within(alert).getByText("409")).toBeInTheDocument();
    expect(within(alert).getByText("conflict")).toBeInTheDocument();
    expect(within(alert).getByText("Conflict")).toBeInTheDocument();
    expect(within(alert).getByText("capability_inheritance_cycle")).toBeInTheDocument();
    expect(within(alert).getByText("/admin/api/v1/llm/inheritance/chat.manager")).toBeInTheDocument();
    expect(within(alert).getByText("req_inline_456")).toBeInTheDocument();
    expect(within(alert).getByText("Parent would create a cycle")).toBeInTheDocument();
    expect(within(alert).getByText("Approval request id: apr_789")).toBeInTheDocument();
    expect(
      within(alert).getByText("Field error: extension value that shares a promoted label"),
    ).toBeInTheDocument();

    fireEvent.click(toggle);

    expect(toggle).toHaveFocus();
    expect(within(alert).queryByText("err_inline_123")).not.toBeInTheDocument();
  });

  it("copies the error id when present", async () => {
    render(<InlineErrorAlert error={displayError} />);

    fireEvent.click(screen.getByRole("button", { name: "Show error details" }));
    fireEvent.click(screen.getByRole("button", { name: "Copy error ID" }));

    expect(navigator.clipboard.writeText).toHaveBeenCalledWith("err_inline_123");
    await waitFor(() => {
      expect(screen.getByText("Copied")).toBeInTheDocument();
    });
  });
});
