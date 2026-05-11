import { createRef } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import SearchField from "./SearchField";

describe("SearchField", () => {
  it("renders a native search input and forwards native props, events, and refs", () => {
    const onChange = vi.fn();
    const ref = createRef<HTMLInputElement>();

    render(
      <SearchField
        ref={ref}
        aria-describedby="search-help"
        aria-label="Find icons"
        autoComplete="off"
        name="iconSearch"
        onChange={onChange}
        placeholder="Search icons"
        value=""
      />,
    );

    const input = screen.getByRole("searchbox", { name: "Find icons" });
    expect(input).toHaveAttribute("type", "search");
    expect(input).toHaveAttribute("autocomplete", "off");
    expect(input).toHaveAttribute("aria-describedby", "search-help");
    expect(input).toHaveAttribute("name", "iconSearch");
    expect(ref.current).toBe(input);

    fireEvent.change(input, { target: { value: "ladder" } });

    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it("supports a visible label plus invalid, disabled, and wrapper class states", () => {
    render(
      <SearchField
        className="asset-search"
        disabled
        invalid
        label="Search assets"
        placeholder="Search"
      />,
    );

    const input = screen.getByRole("searchbox", { name: "Search assets" });
    expect(input).toBeDisabled();
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(input.closest(".search-field")).toHaveClass(
      "asset-search",
      "search-field--disabled",
      "search-field--invalid",
    );
  });
});
