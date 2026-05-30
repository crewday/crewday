import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MemoryRouter } from "react-router-dom";
import PropertyTabs from "./PropertyTabs";

describe("<PropertyTabs>", () => {
  it("links the Inventory related page and marks it active", () => {
    render(
      <MemoryRouter initialEntries={["/w/acme/property/prop_1/inventory"]}>
        <PropertyTabs
          pathname="/w/acme/property/prop_1/inventory"
          propertyId="prop_1"
          activeRelatedPage="inventory"
        />
      </MemoryRouter>,
    );

    const relatedPages = screen.getByRole("navigation", { name: "Related property pages" });
    expect(within(relatedPages).getByRole("link", { name: "Inventory" })).toHaveAttribute(
      "href",
      "/w/acme/property/prop_1/inventory",
    );
    expect(within(relatedPages).getByRole("link", { name: "Inventory" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(within(relatedPages).getByRole("link", { name: "Inventory" })).toHaveClass(
      "page-tabs__tab--active",
    );
  });
});
