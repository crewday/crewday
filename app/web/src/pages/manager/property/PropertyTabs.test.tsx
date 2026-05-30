import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MemoryRouter } from "react-router-dom";
import PropertyTabs from "./PropertyTabs";

describe("<PropertyTabs>", () => {
  it.each([
    ["Assets", "assets"],
    ["Stays", "stays"],
    ["Instructions", "instructions"],
    ["Closures", "closures"],
    ["Inventory", "inventory"],
    ["Schedules", "schedules"],
  ] as const)("links route-backed related pages and marks %s active", (label, key) => {
    render(
      <MemoryRouter initialEntries={["/w/acme/property/prop_1/" + key]}>
        <PropertyTabs
          pathname={"/w/acme/property/prop_1/" + key}
          propertyId="prop_1"
          activeRelatedPage={key}
        />
      </MemoryRouter>,
    );

    const relatedPages = screen.getByRole("navigation", { name: "Related property pages" });
    expect(within(relatedPages).getByRole("link", { name: "Assets" })).toHaveAttribute(
      "href",
      "/w/acme/property/prop_1/assets",
    );
    expect(within(relatedPages).getByRole("link", { name: "Inventory" })).toHaveAttribute(
      "href",
      "/w/acme/property/prop_1/inventory",
    );
    expect(within(relatedPages).getByRole("link", { name: "Schedules" })).toHaveAttribute(
      "href",
      "/w/acme/property/prop_1/schedules",
    );
    expect(within(relatedPages).getByRole("link", { name: "Stays" })).toHaveAttribute(
      "href",
      "/w/acme/property/prop_1/stays",
    );
    expect(within(relatedPages).getByRole("link", { name: "Instructions" })).toHaveAttribute(
      "href",
      "/w/acme/property/prop_1/instructions",
    );
    expect(within(relatedPages).getByRole("link", { name: "Closures" })).toHaveAttribute(
      "href",
      "/w/acme/property/prop_1/closures",
    );
    expect(within(relatedPages).getByRole("link", { name: label })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(within(relatedPages).getByRole("link", { name: label })).toHaveClass(
      "page-tabs__tab--active",
    );
  });
});
