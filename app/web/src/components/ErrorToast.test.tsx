import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ErrorToastProvider } from "./ErrorToast";
import ErrorToastHarness from "./ErrorToastHarness.test-helper";
import type { DisplayError } from "@/lib/displayError";
import { publishGlobalErrorToast } from "@/lib/errorToastBus";

const displayError: DisplayError = {
  id: "err_123",
  message: "Could not save the task.",
  status: 503,
  type: "server_error",
  title: "Service unavailable",
  machineCode: "task_save_failed",
  instance: "/api/v1/tasks/123",
  fieldErrors: [{
    loc: ["body", "name"],
    msg: "Name is required",
    type: "missing",
  }],
  requestId: "req_456",
  raw: null,
  details: [{
    label: "Retry after",
    message: "30 seconds",
    path: null,
    type: null,
  }],
};

beforeEach(() => {
  vi.useFakeTimers();
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText: vi.fn().mockResolvedValue(undefined) },
  });
});

afterEach(() => {
  cleanup();
  vi.runOnlyPendingTimers();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("ErrorToastProvider", () => {
  it("renders a collapsed toast with only the message and close control", () => {
    render(<ErrorToastHarness error={displayError} />);
    fireEvent.click(screen.getByRole("button", { name: "Enqueue error" }));

    const toast = screen.getByRole("status");
    expect(within(toast).getByText("Could not save the task.")).toBeInTheDocument();
    expect(within(toast).getByRole("button", { name: /show error details/i })).toBeInTheDocument();
    expect(within(toast).getByRole("button", { name: "Dismiss error notification" })).toBeInTheDocument();
    expect(within(toast).queryByText("Service unavailable")).not.toBeInTheDocument();
    expect(within(toast).queryByText("Field errors")).not.toBeInTheDocument();
  });

  it("subscribes to global query-client toast events", () => {
    render(
      <ErrorToastProvider>
        <div />
      </ErrorToastProvider>,
    );

    act(() => {
      publishGlobalErrorToast({ error: displayError, source: "query" });
    });

    expect(screen.getByRole("status")).toHaveTextContent("Could not save the task.");
  });

  it("dismisses from the close control", () => {
    render(<ErrorToastHarness error={displayError} />);
    fireEvent.click(screen.getByRole("button", { name: "Enqueue error" }));

    fireEvent.click(screen.getByRole("button", { name: "Dismiss error notification" }));

    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("expands from the message button and exposes normalized properties", () => {
    render(<ErrorToastHarness error={displayError} />);
    fireEvent.click(screen.getByRole("button", { name: "Enqueue error" }));
    fireEvent.click(screen.getByRole("button", { name: /show error details/i }));

    const toast = screen.getByRole("status");
    expect(within(toast).getByText("err_123")).toBeInTheDocument();
    expect(within(toast).getByText("503")).toBeInTheDocument();
    expect(within(toast).getByText("server_error")).toBeInTheDocument();
    expect(within(toast).getByText("Service unavailable")).toBeInTheDocument();
    expect(within(toast).getByText("task_save_failed")).toBeInTheDocument();
    expect(within(toast).getByText("/api/v1/tasks/123")).toBeInTheDocument();
    expect(within(toast).getByText("req_456")).toBeInTheDocument();
    expect(within(toast).getByText("Name is required")).toBeInTheDocument();
    expect(within(toast).getByText("Retry after: 30 seconds")).toBeInTheDocument();
  });

  it("copies the error id when present", () => {
    render(<ErrorToastHarness error={displayError} />);
    fireEvent.click(screen.getByRole("button", { name: "Enqueue error" }));
    fireEvent.click(screen.getByRole("button", { name: /show error details/i }));
    fireEvent.click(screen.getByRole("button", { name: "Copy error ID" }));

    expect(navigator.clipboard.writeText).toHaveBeenCalledWith("err_123");
  });

  it("auto-dismisses only collapsed idle toasts", () => {
    render(<ErrorToastHarness error={displayError} />);
    fireEvent.click(screen.getByRole("button", { name: "Enqueue error" }));

    act(() => {
      vi.advanceTimersByTime(8_999);
    });
    expect(screen.getByRole("status")).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(1);
    });

    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("refreshes the auto-dismiss window when the same error is enqueued again", () => {
    render(<ErrorToastHarness error={displayError} />);
    fireEvent.click(screen.getByRole("button", { name: "Enqueue error" }));
    act(() => {
      vi.advanceTimersByTime(8_000);
    });

    fireEvent.click(screen.getByRole("button", { name: "Enqueue error" }));
    act(() => {
      vi.advanceTimersByTime(8_999);
    });
    expect(screen.getByRole("status")).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(1);
    });

    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("keeps expanded, hovered, and focused toasts until closed", () => {
    render(<ErrorToastHarness error={displayError} />);
    fireEvent.click(screen.getByRole("button", { name: "Enqueue error" }));
    fireEvent.click(screen.getByRole("button", { name: /show error details/i }));
    act(() => {
      vi.advanceTimersByTime(20_000);
    });
    expect(screen.getByRole("status")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Dismiss error notification" }));

    fireEvent.click(screen.getByRole("button", { name: "Enqueue error" }));
    fireEvent.mouseEnter(screen.getByRole("status"));
    act(() => {
      vi.advanceTimersByTime(20_000);
    });
    expect(screen.getByRole("status")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Dismiss error notification" }));

    fireEvent.click(screen.getByRole("button", { name: "Enqueue error" }));
    fireEvent.focus(screen.getByRole("button", { name: /show error details/i }));
    act(() => {
      vi.advanceTimersByTime(20_000);
    });
    expect(screen.getByRole("status")).toBeInTheDocument();
  });

  it("closes the focused toast with Escape", () => {
    render(<ErrorToastHarness error={displayError} />);
    fireEvent.click(screen.getByRole("button", { name: "Enqueue error" }));
    fireEvent.focus(screen.getByRole("button", { name: /show error details/i }));
    fireEvent.keyDown(screen.getByRole("status"), { key: "Escape" });

    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
