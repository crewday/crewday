import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { StatusToastProvider } from "./StatusToast";
import StatusToastHarness from "./StatusToastHarness.test-helper";
import { publishGlobalStatusToast } from "@/lib/statusToastBus";

const MESSAGE = "Completed by Jordan Rivera";

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  cleanup();
  vi.runOnlyPendingTimers();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("StatusToastProvider", () => {
  it("renders an info toast with the message, close control, and polite live region", () => {
    render(<StatusToastHarness message={MESSAGE} />);
    fireEvent.click(screen.getByRole("button", { name: "Enqueue status" }));

    const toast = screen.getByRole("status");
    expect(within(toast).getByText(MESSAGE)).toBeInTheDocument();
    expect(toast).toHaveAttribute("aria-live", "polite");
    expect(toast).toHaveAttribute("data-tone", "info");
    expect(toast).toHaveClass("status-toast--info");
    expect(within(toast).getByRole("button", { name: "Dismiss status notification" })).toBeInTheDocument();
  });

  it("subscribes to the global status-toast bus", () => {
    render(
      <StatusToastProvider>
        <div />
      </StatusToastProvider>,
    );

    act(() => {
      publishGlobalStatusToast({ message: MESSAGE, tone: "info" });
    });

    expect(screen.getByRole("status")).toHaveTextContent(MESSAGE);
  });

  it("dismisses from the close control", () => {
    render(<StatusToastHarness message={MESSAGE} />);
    fireEvent.click(screen.getByRole("button", { name: "Enqueue status" }));

    fireEvent.click(screen.getByRole("button", { name: "Dismiss status notification" }));

    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("auto-dismisses an idle toast", () => {
    render(<StatusToastHarness message={MESSAGE} />);
    fireEvent.click(screen.getByRole("button", { name: "Enqueue status" }));

    act(() => {
      vi.advanceTimersByTime(6_999);
    });
    expect(screen.getByRole("status")).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(1);
    });

    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("pauses auto-dismiss while hovered", () => {
    render(<StatusToastHarness message={MESSAGE} />);
    fireEvent.click(screen.getByRole("button", { name: "Enqueue status" }));

    fireEvent.mouseEnter(screen.getByRole("status"));
    act(() => {
      vi.advanceTimersByTime(20_000);
    });

    expect(screen.getByRole("status")).toBeInTheDocument();
  });

  it("closes with Escape", () => {
    render(<StatusToastHarness message={MESSAGE} />);
    fireEvent.click(screen.getByRole("button", { name: "Enqueue status" }));

    fireEvent.keyDown(screen.getByRole("status"), { key: "Escape" });

    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
