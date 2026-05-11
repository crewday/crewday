import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import SearchableSelect, { type SearchableSelectOption } from "./SearchableSelect";

const OPTIONS: readonly SearchableSelectOption[] = [
  { value: "DE", label: "Germany" },
  { value: "FR", label: "France" },
  { value: "US", label: "United States", searchText: "America" },
];

describe("SearchableSelect", () => {
  it("filters options and commits the active option from the keyboard", () => {
    const onChange = vi.fn();
    render(
      <SearchableSelect
        label="Country"
        value="DE"
        options={OPTIONS}
        onChange={onChange}
        required
      />,
    );

    const input = screen.getByRole("combobox", { name: /country/i });
    expect(input).toHaveValue("Germany");

    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: "fra" } });

    expect(screen.getByRole("option", { name: /france/i })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /germany/i })).not.toBeInTheDocument();

    fireEvent.keyDown(input, { key: "Enter" });

    expect(onChange).toHaveBeenCalledWith("FR");
  });

  it("resets uncommitted search text to the selected option on blur", () => {
    const onChange = vi.fn();
    render(
      <SearchableSelect
        label="Country"
        value="DE"
        options={OPTIONS}
        onChange={onChange}
      />,
    );

    const input = screen.getByRole("combobox", { name: /country/i });
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: "zz" } });
    expect(input).toHaveValue("zz");

    fireEvent.blur(input);

    expect(input).toHaveValue("Germany");
    expect(onChange).not.toHaveBeenCalled();
  });
});
