import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  __resetApiProvidersForTests,
  registerWorkspaceSlugGetter,
} from "@/lib/api";
import { installFetchRoutes } from "@/test/helpers";
import { chooseSearchableOption } from "@/test/searchableSelect";
import { BookingProposeDialog } from "./BookingProposeDialog";

const originalShowModal = HTMLDialogElement.prototype.showModal;
const originalClose = HTMLDialogElement.prototype.close;

function properties(): { id: string; name: string; timezone: string; city: string }[] {
  return [
    { id: "prop_1", name: "Harbor Loft", city: "Nice", timezone: "Europe/Paris" },
    { id: "prop_2", name: "Garden Cottage", city: "Lyon", timezone: "Europe/Paris" },
    { id: "prop_3", name: "Market Studio", city: "Paris", timezone: "Europe/Paris" },
    { id: "prop_4", name: "Canal House", city: "Annecy", timezone: "Europe/Paris" },
    { id: "prop_5", name: "Hilltop Flat", city: "Grasse", timezone: "Europe/Paris" },
    { id: "prop_6", name: "Station Suite", city: "Marseille", timezone: "Europe/Paris" },
  ];
}

function bookingBody(): unknown {
  return {
    id: "booking_1",
    property_id: "prop_2",
    scheduled_start: "2026-05-12T09:00:00",
    scheduled_end: "2026-05-12T12:00:00",
    notes_md: "Covered the laundry pickup.",
    status: "pending_approval",
  };
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

describe("BookingProposeDialog", () => {
  it("submits the selected searchable property id", async () => {
    const env = installFetchRoutes(
      { "/api/v1/bookings": [{ status: 201, body: bookingBody() }] },
      { match: "endsWith" },
    );
    const onClose = vi.fn();
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });

    render(
      <QueryClientProvider client={qc}>
        <BookingProposeDialog
          iso="2026-05-12"
          properties={properties()}
          onClose={onClose}
        />
      </QueryClientProvider>,
    );

    const dialog = screen.getByRole("dialog", { name: "Propose ad-hoc booking" });
    await waitFor(() => {
      expect(within(dialog).getByRole("combobox", { name: /^Property\b/ })).toHaveValue("Harbor Loft");
    });
    const propertyControl = within(dialog).getByRole("combobox", { name: /^Property\b/ });
    const form = propertyControl.closest("form") as HTMLFormElement;
    const propertyInput = form.querySelector<HTMLInputElement>('input[type="hidden"][name="property_id"]');
    expect(propertyControl).toBeRequired();
    expect(propertyInput).toHaveValue("prop_1");

    await chooseSearchableOption(dialog, /^Property\b/, /Garden Cottage/i);
    expect(propertyInput).toHaveValue("prop_2");
    fireEvent.change(within(dialog).getByLabelText(/^Notes\b/), {
      target: { value: "Covered the laundry pickup." },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Propose" }));

    await waitFor(() => {
      expect(env.calls.find((call) => call.url.endsWith("/api/v1/bookings"))).toBeDefined();
    });

    const postCall = env.calls.find((call) => call.url.endsWith("/api/v1/bookings"));
    expect(postCall?.init.method).toBe("POST");
    expect(JSON.parse(postCall?.init.body as string)).toEqual({
      property_id: "prop_2",
      scheduled_start: "2026-05-12T09:00:00",
      scheduled_end: "2026-05-12T12:00:00",
      notes_md: "Covered the laundry pickup.",
    });
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
