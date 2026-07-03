import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { __resetApiProvidersForTests, registerWorkspaceSlugGetter } from "@/lib/api";
import { setGlobalErrorToastHandler } from "@/lib/errorToastBus";
import { installFetchRoutes } from "@/test/helpers";
import type { Booking } from "@/types/api";
import { BookingDeclineDialog } from "./BookingDeclineDialog";

const originalShowModal = HTMLDialogElement.prototype.showModal;
const originalClose = HTMLDialogElement.prototype.close;

function booking(): Booking {
  return {
    id: "booking_1",
    employee_id: "emp_1",
    property_id: "prop_2",
    scheduled_start: "2026-05-12T09:00:00",
    scheduled_end: "2026-05-12T12:00:00",
    status: "scheduled",
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
      <BookingDeclineDialog booking={booking()} propertyLabel="Garden Cottage" onClose={onClose} />
    </QueryClientProvider>,
  );
  return { onClose, dialog: screen.getByRole("dialog", { name: "Decline booking" }) };
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

describe("BookingDeclineDialog", () => {
  it("posts the worker's reason", async () => {
    const env = installFetchRoutes(
      { "/api/v1/bookings/booking_1/decline": [{ status: 200, body: booking() }] },
      { match: "endsWith" },
    );
    const { onClose, dialog } = renderDialog();

    fireEvent.change(within(dialog).getByRole("textbox", { name: "Reason" }), {
      target: { value: "Off sick today" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Decline booking" }));

    await waitFor(() => {
      expect(env.calls.some((c) => c.url.endsWith("/api/v1/bookings/booking_1/decline"))).toBe(true);
    });
    const post = env.calls.find((c) => c.url.endsWith("/api/v1/bookings/booking_1/decline"));
    expect(post?.init.method).toBe("POST");
    expect(JSON.parse(post?.init.body as string)).toEqual({ reason: "Off sick today" });
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });

  it("blocks submit and shows a validation message when the reason is empty", () => {
    const env = installFetchRoutes({}, { match: "endsWith" });
    const { dialog } = renderDialog();

    fireEvent.click(within(dialog).getByRole("button", { name: "Decline booking" }));

    expect(within(dialog).getByRole("alert")).toHaveTextContent(/why/i);
    expect(env.calls).toHaveLength(0);
  });

  it("surfaces a toast and stays open when the decline request fails", async () => {
    installFetchRoutes(
      { "/api/v1/bookings/booking_1/decline": [{ status: 500, body: { detail: "boom" } }] },
      { match: "endsWith" },
    );
    const toasted = vi.fn();
    const unsubscribe = setGlobalErrorToastHandler(toasted);
    const { onClose, dialog } = renderDialog();

    fireEvent.change(within(dialog).getByRole("textbox", { name: "Reason" }), {
      target: { value: "Can't make it" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Decline booking" }));

    await waitFor(() => expect(toasted).toHaveBeenCalledTimes(1));
    expect(toasted.mock.calls[0]![0].source).toBe("mutation");
    expect(onClose).not.toHaveBeenCalled();
    unsubscribe();
  });
});
