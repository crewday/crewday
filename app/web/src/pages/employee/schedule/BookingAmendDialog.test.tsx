import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { __resetApiProvidersForTests, registerWorkspaceSlugGetter } from "@/lib/api";
import { setGlobalErrorToastHandler } from "@/lib/errorToastBus";
import { installFetchRoutes } from "@/test/helpers";
import type { Booking } from "@/types/api";
import { BookingAmendDialog } from "./BookingAmendDialog";

const originalShowModal = HTMLDialogElement.prototype.showModal;
const originalClose = HTMLDialogElement.prototype.close;

// scheduled 09:00–12:00, no break → bookingMinutes = 180.
function booking(): Booking {
  return {
    id: "booking_1",
    employee_id: "emp_1",
    property_id: "prop_2",
    scheduled_start: "2026-05-12T09:00:00",
    scheduled_end: "2026-05-12T12:00:00",
    status: "completed",
    kind: "work",
    actual_minutes: null,
    actual_minutes_paid: null,
    break_seconds: 0,
    pending_amend_minutes: null,
    pending_amend_reason: null,
    declined_at: null,
    declined_reason: null,
    notes_md: "",
    adjusted: false,
    adjustment_reason: null,
    client_org_id: null,
    work_engagement_id: "we_1",
    user_id: "user_1",
  };
}

function newClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function renderDialog(onClose = vi.fn()) {
  const qc = newClient();
  render(
    <QueryClientProvider client={qc}>
      <BookingAmendDialog booking={booking()} propertyLabel="Garden Cottage" onClose={onClose} />
    </QueryClientProvider>,
  );
  return { onClose, dialog: screen.getByRole("dialog", { name: "Amend booking" }) };
}

beforeEach(() => {
  __resetApiProvidersForTests();
  registerWorkspaceSlugGetter(() => "acme");
  HTMLDialogElement.prototype.showModal = function showModal() {
    this.open = true;
  };
  HTMLDialogElement.prototype.close = function close() {
    this.open = false;
  };
});

afterEach(() => {
  cleanup();
  __resetApiProvidersForTests();
  HTMLDialogElement.prototype.showModal = originalShowModal;
  HTMLDialogElement.prototype.close = originalClose;
  vi.restoreAllMocks();
});

describe("BookingAmendDialog", () => {
  it("prefills the booking's computed minutes and posts the worker's input", async () => {
    const env = installFetchRoutes(
      { "/api/v1/bookings/booking_1/amend": [{ status: 200, body: booking() }] },
      { match: "endsWith" },
    );
    const { onClose, dialog } = renderDialog();

    const minutes = within(dialog).getByRole("spinbutton", { name: "Minutes worked" });
    expect(minutes).toHaveValue(180);

    fireEvent.change(minutes, { target: { value: "205" } });
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Reason" }), {
      target: { value: "Deep clean overran" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Submit amend" }));

    await waitFor(() => {
      expect(env.calls.some((c) => c.url.endsWith("/api/v1/bookings/booking_1/amend"))).toBe(true);
    });
    const post = env.calls.find((c) => c.url.endsWith("/api/v1/bookings/booking_1/amend"));
    expect(post?.init.method).toBe("POST");
    expect(JSON.parse(post?.init.body as string)).toEqual({
      actual_minutes: 205,
      reason: "Deep clean overran",
    });
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });

  it("blocks submit and shows a validation message when the reason is empty", () => {
    const env = installFetchRoutes({}, { match: "endsWith" });
    const { dialog } = renderDialog();

    fireEvent.click(within(dialog).getByRole("button", { name: "Submit amend" }));

    expect(within(dialog).getByRole("alert")).toHaveTextContent(/reason/i);
    expect(env.calls).toHaveLength(0);
  });

  it("blocks submit when minutes are out of range", () => {
    const env = installFetchRoutes({}, { match: "endsWith" });
    const { dialog } = renderDialog();

    fireEvent.change(within(dialog).getByRole("spinbutton", { name: "Minutes worked" }), {
      target: { value: "0" },
    });
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Reason" }), {
      target: { value: "Left early" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Submit amend" }));

    expect(within(dialog).getByRole("alert")).toHaveTextContent(/minutes/i);
    expect(env.calls).toHaveLength(0);
  });

  it("surfaces a toast and stays open when the amend request fails", async () => {
    installFetchRoutes(
      { "/api/v1/bookings/booking_1/amend": [{ status: 500, body: { detail: "boom" } }] },
      { match: "endsWith" },
    );
    const toasted = vi.fn();
    const unsubscribe = setGlobalErrorToastHandler(toasted);
    const { onClose, dialog } = renderDialog();

    fireEvent.change(within(dialog).getByRole("textbox", { name: "Reason" }), {
      target: { value: "Stayed late" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Submit amend" }));

    await waitFor(() => expect(toasted).toHaveBeenCalledTimes(1));
    expect(toasted.mock.calls[0]![0].source).toBe("mutation");
    expect(onClose).not.toHaveBeenCalled();
    unsubscribe();
  });
});
