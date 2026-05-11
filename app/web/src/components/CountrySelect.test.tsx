import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import CountrySelect from "./CountrySelect";

describe("CountrySelect", () => {
  it("renders a searchable country field that submits the selected country code", () => {
    render(<CountrySelectHarness />);

    const input = screen.getByRole("combobox", { name: /country/i });
    const form = screen.getByTestId("country-form") as HTMLFormElement;
    expect(input).toHaveValue("United States");
    expect(input).toBeRequired();
    expect(new FormData(form).get("country")).toBe("US");

    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: "Germany" } });
    fireEvent.keyDown(input, { key: "Enter" });

    expect(input).toHaveValue("Germany");
    expect(new FormData(form).get("country")).toBe("DE");
  });

  it("forwards described-by, invalid, disabled, and class props", () => {
    render(
      <CountrySelect
        value="US"
        onChange={() => undefined}
        name="country"
        disabled
        className="property-country"
        inputClassName="property-country__input"
        aria-describedby="country-error"
        aria-invalid
      />,
    );

    const input = screen.getByRole("combobox", { name: /country/i });
    expect(input).toBeDisabled();
    expect(input).toHaveClass("property-country__input");
    expect(input.closest("label")).toHaveClass("property-country");
    expect(input).toHaveAttribute("aria-describedby", "country-error");
    expect(input).toHaveAttribute("aria-invalid", "true");
  });
});

function CountrySelectHarness() {
  const [value, setValue] = useState("US");
  return (
    <form data-testid="country-form">
      <CountrySelect value={value} onChange={setValue} name="country" required />
    </form>
  );
}
