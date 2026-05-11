import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import SearchableSelect, { type SearchableSelectOption } from "./SearchableSelect";

const OPTIONS: readonly SearchableSelectOption[] = [
  { value: "DE", label: "Germany" },
  { value: "FR", label: "France" },
  { value: "US", label: "United States", searchText: "America" },
];

const LONG_OPTIONS: readonly SearchableSelectOption[] = Array.from({ length: 45 }, (_, index) => ({
  value: `option-${index}`,
  label: `Option ${index.toString().padStart(2, "0")}`,
}));

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

  it("tracks the active descendant while arrowing through open options", () => {
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
    fireEvent.keyDown(input, { key: "ArrowDown" });

    const activeOption = screen.getByRole("option", { name: /france/i });
    expect(input).toHaveAttribute("aria-activedescendant", activeOption.id);

    fireEvent.keyDown(input, { key: "Enter" });

    expect(onChange).toHaveBeenCalledWith("FR");
  });

  it("keeps the selected option active when focus opens the full option list", () => {
    const onChange = vi.fn();
    render(
      <SearchableSelect
        label="Country"
        value="US"
        options={OPTIONS}
        onChange={onChange}
      />,
    );

    const input = screen.getByRole("combobox", { name: /country/i });
    fireEvent.focus(input);

    const activeOption = screen.getByRole("option", { name: /united states/i });
    expect(input).toHaveAttribute("aria-activedescendant", activeOption.id);

    fireEvent.keyDown(input, { key: "Enter" });

    expect(onChange).toHaveBeenCalledWith("US");
  });

  it("keeps a selected option beyond the visible limit active when the list opens", () => {
    const onChange = vi.fn();
    render(
      <SearchableSelect
        label="Country"
        value="option-42"
        options={LONG_OPTIONS}
        onChange={onChange}
      />,
    );

    const input = screen.getByRole("combobox", { name: /country/i });
    fireEvent.focus(input);

    const activeOption = screen.getByRole("option", { name: /option 42/i });
    expect(input).toHaveAttribute("aria-activedescendant", activeOption.id);

    fireEvent.keyDown(input, { key: "Enter" });

    expect(onChange).toHaveBeenCalledWith("option-42");
  });

  it("resets uncommitted search text and closes on Escape", () => {
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

    fireEvent.keyDown(input, { key: "Escape" });

    expect(input).toHaveValue("Germany");
    expect(input).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();
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

  it("submits the selected value while preserving classes and descriptions", () => {
    render(<SearchableSelectHarness />);

    const input = screen.getByRole("combobox", { name: /country/i });
    const form = screen.getByTestId("select-form") as HTMLFormElement;
    expect(input).not.toHaveAttribute("name");
    expect(input).toHaveClass("country-select__input");
    expect(input.closest("label")).toHaveClass("country-select");
    expect(input).toHaveAttribute("aria-describedby", "server-error country-help");
    expect(new FormData(form).get("country")).toBe("DE");

    fireEvent.focus(input);
    fireEvent.mouseDown(screen.getByRole("option", { name: /france/i }));

    expect(input).toHaveValue("France");
    expect(new FormData(form).get("country")).toBe("FR");
  });

  it("keeps a required blank option invalid until a real option is selected", () => {
    render(<SearchableSelectHarness initialValue="" required />);

    const input = screen.getByRole("combobox", { name: /country/i });
    expect(input).toHaveValue("Choose country");
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(input).toBeInvalid();

    fireEvent.focus(input);
    fireEvent.mouseDown(screen.getByRole("option", { name: /germany/i }));

    expect(input).toHaveValue("Germany");
    expect(input).not.toHaveAttribute("aria-invalid");
    expect(input).toBeValid();
  });

  it("marks a cleared required selection invalid until blur restores the selected option", () => {
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
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: "" } });

    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(input).toBeInvalid();

    fireEvent.blur(input);

    expect(input).toHaveValue("Germany");
    expect(input).not.toHaveAttribute("aria-invalid");
    expect(onChange).not.toHaveBeenCalled();
  });

  it("does not open or submit a disabled select", () => {
    render(<SearchableSelectHarness disabled />);

    const input = screen.getByRole("combobox", { name: /country/i });
    const form = screen.getByTestId("select-form") as HTMLFormElement;
    expect(input).toBeDisabled();
    expect(input.closest("label")).toHaveClass("searchable-select--disabled");

    fireEvent.focus(input);

    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(new FormData(form).has("country")).toBe(false);
  });
});

interface SearchableSelectHarnessProps {
  disabled?: boolean;
  initialValue?: string;
  required?: boolean;
}

function SearchableSelectHarness({
  disabled = false,
  initialValue = "DE",
  required = false,
}: SearchableSelectHarnessProps) {
  const [value, setValue] = useState(initialValue);
  return (
    <form data-testid="select-form">
      <SearchableSelect
        label="Country"
        value={value}
        options={OPTIONS}
        onChange={setValue}
        name="country"
        disabled={disabled}
        required={required}
        className="country-select"
        inputClassName="country-select__input"
        helpId="country-help"
        helpText="Used for property defaults."
        blankOption={{ label: "Choose country", secondaryText: "Required" }}
        renderOptionSecondaryText={(option) => option.secondaryText ?? option.value}
        aria-describedby="server-error"
      />
    </form>
  );
}
