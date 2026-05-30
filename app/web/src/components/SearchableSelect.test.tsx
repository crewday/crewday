import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import SearchableSelect, { type SearchableSelectOption } from "./SearchableSelect";
import formsCss from "@/styles/forms.css?raw";

const OPTIONS: readonly SearchableSelectOption[] = [
  { value: "DE", label: "Germany" },
  { value: "FR", label: "France" },
  { value: "US", label: "United States", searchText: "America" },
];

const LONG_OPTIONS: readonly SearchableSelectOption[] = Array.from({ length: 45 }, (_, index) => ({
  value: `option-${index}`,
  label: `Option ${index.toString().padStart(2, "0")}`,
}));

afterEach(() => {
  vi.restoreAllMocks();
});

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

  it("keeps open popover keys local and lets closed Enter and Escape bubble", () => {
    const onChange = vi.fn();
    const onKeyDown = vi.fn();
    render(
      <div onKeyDown={onKeyDown}>
        <SearchableSelect
          label="Country"
          value="DE"
          options={OPTIONS}
          onChange={onChange}
        />
      </div>,
    );

    const input = screen.getByRole("combobox", { name: /country/i });
    fireEvent.focus(input);

    fireEvent.keyDown(input, { key: "Escape" });

    expect(input).toHaveAttribute("aria-expanded", "false");
    expect(onChange).not.toHaveBeenCalled();
    expect(onKeyDown).not.toHaveBeenCalled();

    fireEvent.focus(input);
    fireEvent.keyDown(input, { key: "Enter" });

    expect(onChange).toHaveBeenCalledWith("DE");
    expect(onKeyDown).not.toHaveBeenCalled();

    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.keyDown(input, { key: "Escape" });

    expect(onKeyDown).toHaveBeenCalledTimes(2);
    expect(onKeyDown.mock.calls.map(([event]) => event.key)).toEqual(["Enter", "Escape"]);
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

  it("keeps portaled listbox aria relationships and mouse selection wired", () => {
    render(<SearchableSelectHarness />);

    const input = screen.getByRole("combobox", { name: /country/i });
    fireEvent.focus(input);

    const listbox = screen.getByRole("listbox", { name: /country/i });
    const france = screen.getByRole("option", { name: /france/i });
    expect(listbox.parentElement?.parentElement).toBe(document.body);
    expect(input).toHaveAttribute("aria-controls", listbox.id);
    expect(input).toHaveAttribute("aria-activedescendant", screen.getByRole("option", { name: /germany/i }).id);

    fireEvent.mouseEnter(france);
    expect(input).toHaveAttribute("aria-activedescendant", france.id);

    fireEvent.mouseDown(france);
    expect(input).toHaveValue("France");
    expect(screen.queryByRole("listbox", { name: /country/i })).not.toBeInTheDocument();
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

  it("does not mark disabled required blanks invalid", () => {
    render(<SearchableSelectHarness disabled initialValue="" required />);

    const input = screen.getByRole("combobox", { name: /country/i });

    expect(input).toBeDisabled();
    expect(input).not.toHaveAttribute("aria-invalid");
  });

  it("shows disabled options without allowing them to be selected", () => {
    const onChange = vi.fn();
    render(
      <SearchableSelect
        label="Country"
        value="DE"
        options={[
          { value: "DE", label: "Germany" },
          { value: "FR", label: "France", disabled: true, secondaryText: "Unavailable" },
          { value: "US", label: "United States" },
        ]}
        onChange={onChange}
      />,
    );

    const input = screen.getByRole("combobox", { name: /country/i });
    fireEvent.focus(input);

    const disabledOption = screen.getByRole("option", { name: /france/i });
    const selectedOption = screen.getByRole("option", { name: /germany/i });
    expect(disabledOption).toHaveAttribute("aria-disabled", "true");

    fireEvent.mouseEnter(disabledOption);
    expect(input).toHaveAttribute("aria-activedescendant", selectedOption.id);

    fireEvent.mouseDown(disabledOption);
    expect(onChange).not.toHaveBeenCalled();
    expect(input).toHaveValue("Germany");

    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(input).toHaveAttribute(
      "aria-activedescendant",
      screen.getByRole("option", { name: /united states/i }).id,
    );
  });

  it("skips a disabled filtered option when committing from the keyboard", () => {
    const onChange = vi.fn();
    render(
      <SearchableSelect
        label="Country"
        value="DE"
        options={[
          { value: "DE", label: "Germany" },
          { value: "FR", label: "France", disabled: true },
          { value: "FRA", label: "Frankfurt" },
        ]}
        onChange={onChange}
      />,
    );

    const input = screen.getByRole("combobox", { name: /country/i });
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: "fr" } });

    const enabledOption = screen.getByRole("option", { name: /frankfurt/i });
    expect(input).toHaveAttribute("aria-activedescendant", enabledOption.id);

    fireEvent.keyDown(input, { key: "Enter" });

    expect(onChange).toHaveBeenCalledWith("FRA");
  });

  it("keeps long labels and secondary text inside the popover", () => {
    expect(formsCss).toMatch(
      /\.searchable-select__label \{[\s\S]*overflow-wrap: anywhere;[\s\S]*\}/m,
    );
    expect(formsCss).toMatch(
      /\.searchable-select__value \{[\s\S]*max-width: 45%;[\s\S]*overflow-wrap: anywhere;[\s\S]*\}/m,
    );
  });

  it("portals the popover above clipped inline tables when there is not enough table space below", () => {
    mockViewport({ width: 1440, height: 1000 });
    mockRects({
      input: rect({ top: 260, bottom: 300, left: 420, right: 620, width: 200, height: 40 }),
      table: rect({ top: 80, bottom: 314, left: 300, right: 900, width: 600, height: 234 }),
    });

    render(
      <div className="inline-table-form__table" data-testid="inline-table">
        <SearchableSelect
          label="Template"
          value=""
          options={OPTIONS}
          onChange={vi.fn()}
          placeholder="Select template"
        />
      </div>,
    );

    fireEvent.focus(screen.getByRole("combobox", { name: /template/i }));

    const listbox = screen.getByRole("listbox", { name: /template/i });
    const popover = listbox.closest(".searchable-select__popover");
    expect(popover?.parentElement).toBe(document.body);
    expect(popover).toHaveClass("searchable-select__popover--above");
    expect(popover).toHaveAttribute("data-placement", "above");
    expect(popover).toHaveStyle({ left: "420px", width: "200px" });
  });

  it("keeps the popover below inline table controls when there is enough table space", () => {
    mockViewport({ width: 1440, height: 1000 });
    mockRects({
      input: rect({ top: 120, bottom: 160, left: 420, right: 620, width: 200, height: 40 }),
      table: rect({ top: 80, bottom: 620, left: 300, right: 900, width: 600, height: 540 }),
    });

    render(
      <div className="inline-table-form__table">
        <SearchableSelect
          label="Property"
          value=""
          options={OPTIONS}
          onChange={vi.fn()}
          placeholder="Select property"
        />
      </div>,
    );

    fireEvent.focus(screen.getByRole("combobox", { name: /property/i }));

    const popover = screen.getByRole("listbox", { name: /property/i }).closest(".searchable-select__popover");
    expect(popover).toHaveClass("searchable-select__popover--below");
    expect(popover).toHaveAttribute("data-placement", "below");
    expect(popover).toHaveStyle({ top: "168px", width: "200px" });
  });

  it("updates fixed popover placement on layout changes and removes window listeners when closed", () => {
    mockViewport({ width: 1440, height: 1000 });
    const inputRect = mutableRect({ top: 120, bottom: 160, left: 420, right: 620, width: 200, height: 40 });
    const tableRect = mutableRect({ top: 80, bottom: 620, left: 300, right: 900, width: 600, height: 540 });
    mockRects({ input: inputRect, table: tableRect });
    const addListener = vi.spyOn(window, "addEventListener");
    const removeListener = vi.spyOn(window, "removeEventListener");

    render(
      <div className="inline-table-form__table">
        <SearchableSelect
          label="Template"
          value=""
          options={OPTIONS}
          onChange={vi.fn()}
          placeholder="Select template"
        />
      </div>,
    );

    const input = screen.getByRole("combobox", { name: /template/i });
    fireEvent.focus(input);
    expect(screen.getByRole("listbox", { name: /template/i }).closest(".searchable-select__popover"))
      .toHaveAttribute("data-placement", "below");
    expect(addListener).toHaveBeenCalledWith("resize", expect.any(Function));
    expect(addListener).toHaveBeenCalledWith("scroll", expect.any(Function), true);

    inputRect.top = 260;
    inputRect.bottom = 300;
    tableRect.bottom = 314;
    fireEvent.scroll(window);

    expect(screen.getByRole("listbox", { name: /template/i }).closest(".searchable-select__popover"))
      .toHaveAttribute("data-placement", "above");

    fireEvent.keyDown(input, { key: "Escape" });

    expect(screen.queryByRole("listbox", { name: /template/i })).not.toBeInTheDocument();
    expect(removeListener).toHaveBeenCalledWith("resize", expect.any(Function));
    expect(removeListener).toHaveBeenCalledWith("scroll", expect.any(Function), true);
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

interface MockViewport {
  width: number;
  height: number;
}

interface MockRects {
  input: DOMRect;
  table: DOMRect;
}

function mockViewport({ width, height }: MockViewport) {
  Object.defineProperty(window, "innerWidth", { configurable: true, value: width });
  Object.defineProperty(window, "innerHeight", { configurable: true, value: height });
}

function mockRects({ input, table }: MockRects) {
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function getBoundingClientRect() {
    if (this.classList.contains("inline-table-form__table")) return table;
    if (this.getAttribute("role") === "combobox") return input;
    return rect({ top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 });
  });
}

interface RectValues {
  top: number;
  bottom: number;
  left: number;
  right: number;
  width: number;
  height: number;
}

function rect(values: RectValues): DOMRect {
  return {
    ...values,
    x: values.left,
    y: values.top,
    toJSON: () => values,
  } as DOMRect;
}

function mutableRect(values: RectValues): DOMRect {
  return values as DOMRect;
}
