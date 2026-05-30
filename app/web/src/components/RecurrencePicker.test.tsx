import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useState } from "react";

import RecurrencePicker from "@/components/RecurrencePicker";

function Harness({ initial = null }: { initial?: string | null }) {
  const [value, setValue] = useState<string | null>(initial);
  return <RecurrencePicker value={value} onChange={setValue} />;
}

beforeEach(() => {
  HTMLDialogElement.prototype.showModal = function showModal() {
    this.open = true;
  };
  HTMLDialogElement.prototype.close = function close() {
    this.open = false;
  };
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("<RecurrencePicker>", () => {
  it("edits friendly recurrence and shows a preview", () => {
    render(<Harness />);

    fireEvent.click(screen.getByRole("button", { name: "Recurrence" }));
    const dialog = screen.getByRole("dialog", { name: "Recurrence" });
    fireEvent.change(within(dialog).getByLabelText(/^Repeats\b/), {
      target: { value: "weekly" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Wed" }));

    expect(within(dialog).getByText("Next occurrences")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Apply" }));

    expect(screen.getByRole("button", { name: "Recurrence" })).toHaveTextContent("Weekly on Mon, Wed");
  });

  it("blocks invalid advanced RRULE values before applying", () => {
    render(<Harness initial="FREQ=MONTHLY;BYMONTHDAY=1" />);

    fireEvent.click(screen.getByRole("button", { name: "Recurrence" }));
    const dialog = screen.getByRole("dialog", { name: "Recurrence" });
    fireEvent.click(within(dialog).getByRole("button", { name: /Advanced/ }));
    fireEvent.change(within(dialog).getByLabelText(/^Raw RRULE\b/), {
      target: { value: "FREQ=HOURLY" },
    });

    expect(within(dialog).getByText("Use FREQ=DAILY, WEEKLY, MONTHLY, or YEARLY.")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Apply" })).toBeDisabled();
    expect(within(dialog).getByText("Fix the RRULE to preview upcoming dates.")).toBeInTheDocument();
  });

  it("blocks unsupported RRULE keys and previews bounded advanced rules accurately", () => {
    render(<Harness initial="FREQ=DAILY;COUNT=2" />);

    fireEvent.click(screen.getByRole("button", { name: "Recurrence" }));
    const dialog = screen.getByRole("dialog", { name: "Recurrence" });
    fireEvent.click(within(dialog).getByRole("button", { name: /Advanced/ }));

    expect(within(dialog).getAllByRole("listitem")).toHaveLength(2);

    fireEvent.change(within(dialog).getByLabelText(/^Raw RRULE\b/), {
      target: { value: "FREQ=WEEKLY;NOPE=1" },
    });

    expect(within(dialog).getByText("RRULE key NOPE is not supported.")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Apply" })).toBeDisabled();
  });
});
