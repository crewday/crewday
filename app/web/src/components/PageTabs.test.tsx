import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import PageTabs, { type PageTab } from "./PageTabs";

const TABS: PageTab[] = [
  { key: "overview", label: "Overview", panelId: "overview-panel" },
  { key: "activity", label: "Activity", panelId: "activity-panel" },
  { key: "billing", label: "Billing", panelId: "billing-panel" },
];

function renderHashTabs(defaultKey = "overview") {
  return render(<PageTabs ariaLabel="Property sections" tabs={TABS} hashBacked defaultKey={defaultKey} />);
}

beforeEach(() => {
  window.history.replaceState(null, "", "/");
});

afterEach(() => {
  window.history.replaceState(null, "", "/");
});

describe("<PageTabs>", () => {
  it("selects the safe default tab when no hash is present", () => {
    renderHashTabs();

    const tablist = screen.getByRole("tablist", { name: "Property sections" });
    expect(within(tablist).getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
    expect(within(tablist).getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-controls", "overview-panel");
    expect(within(tablist).getByRole("tab", { name: "Activity" })).toHaveAttribute("aria-selected", "false");
  });

  it("falls back to the default tab for an invalid hash", () => {
    window.history.replaceState(null, "", "/property/1#missing");

    renderHashTabs("activity");

    expect(screen.getByRole("tab", { name: "Activity" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Billing" })).toHaveAttribute("aria-selected", "false");
  });

  it("falls back to the default tab for a malformed hash", () => {
    window.history.replaceState(null, "", "/property/1#%");

    renderHashTabs("activity");

    expect(screen.getByRole("tab", { name: "Activity" })).toHaveAttribute("aria-selected", "true");
  });

  it("syncs selected tab from hashchange events", () => {
    renderHashTabs();

    window.history.pushState(null, "", "/property/1#billing");
    fireEvent(window, new HashChangeEvent("hashchange"));

    expect(screen.getByRole("tab", { name: "Billing" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "false");
  });

  it("updates the hash when an in-place tab is clicked", () => {
    const onSelect = vi.fn();
    render(<PageTabs ariaLabel="Property sections" tabs={TABS} hashBacked onSelect={onSelect} />);

    fireEvent.click(screen.getByRole("tab", { name: "Billing" }));

    expect(window.location.hash).toBe("#billing");
    expect(onSelect).toHaveBeenCalledWith("billing");
    expect(screen.getByRole("tab", { name: "Billing" })).toHaveAttribute("aria-selected", "true");
  });

  it("follows browser Back and Forward hash navigation", async () => {
    renderHashTabs();

    fireEvent.click(screen.getByRole("tab", { name: "Activity" }));
    fireEvent.click(screen.getByRole("tab", { name: "Billing" }));
    expect(screen.getByRole("tab", { name: "Billing" })).toHaveAttribute("aria-selected", "true");

    window.history.back();
    await waitFor(() => {
      expect(window.location.hash).toBe("#activity");
      expect(screen.getByRole("tab", { name: "Activity" })).toHaveAttribute("aria-selected", "true");
    });

    window.history.forward();
    await waitFor(() => {
      expect(window.location.hash).toBe("#billing");
      expect(screen.getByRole("tab", { name: "Billing" })).toHaveAttribute("aria-selected", "true");
    });
  });

  it("supports Left, Right, Home, and End keyboard navigation", () => {
    renderHashTabs();

    const overview = screen.getByRole("tab", { name: "Overview" });
    overview.focus();

    fireEvent.keyDown(screen.getByRole("tablist", { name: "Property sections" }), { key: "ArrowRight" });
    expect(screen.getByRole("tab", { name: "Activity" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Activity" })).toHaveFocus();

    fireEvent.keyDown(screen.getByRole("tablist", { name: "Property sections" }), { key: "End" });
    expect(screen.getByRole("tab", { name: "Billing" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Billing" })).toHaveFocus();

    fireEvent.keyDown(screen.getByRole("tablist", { name: "Property sections" }), { key: "ArrowLeft" });
    expect(screen.getByRole("tab", { name: "Activity" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Activity" })).toHaveFocus();

    fireEvent.keyDown(screen.getByRole("tablist", { name: "Property sections" }), { key: "Home" });
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveFocus();
  });

  it("skips disabled in-place tabs for click and keyboard selection", () => {
    const onSelect = vi.fn();
    render(
      <PageTabs
        ariaLabel="Property sections"
        tabs={[
          { key: "overview", label: "Overview" },
          { key: "activity", label: "Activity", disabled: true },
          { key: "billing", label: "Billing" },
        ]}
        hashBacked
        onSelect={onSelect}
      />,
    );

    fireEvent.click(screen.getByRole("tab", { name: "Activity" }));
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
    expect(onSelect).not.toHaveBeenCalled();
    expect(window.location.hash).toBe("");

    screen.getByRole("tab", { name: "Overview" }).focus();
    fireEvent.keyDown(screen.getByRole("tablist", { name: "Property sections" }), { key: "ArrowRight" });
    expect(screen.getByRole("tab", { name: "Billing" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Billing" })).toHaveFocus();
    expect(onSelect).toHaveBeenCalledWith("billing");
  });

  it("keeps route destinations as links instead of tabs", () => {
    render(
      <MemoryRouter>
        <PageTabs
          ariaLabel="Employee pages"
          activeKey="profile"
          tabs={[
            { key: "profile", label: "Profile", to: "/user/emp_1" },
            { key: "leaves", label: "Leaves", to: "/user/emp_1/leaves" },
          ]}
        />
      </MemoryRouter>,
    );

    const nav = screen.getByRole("navigation", { name: "Employee pages" });
    expect(within(nav).getByRole("link", { name: "Profile" })).toHaveAttribute("href", "/user/emp_1");
    expect(within(nav).getByRole("link", { name: "Profile" })).toHaveAttribute("aria-current", "page");
    expect(screen.queryByRole("tab")).not.toBeInTheDocument();
  });
});
