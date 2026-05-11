import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import TimezoneSelect from "./TimezoneSelect";

describe("TimezoneSelect", () => {
  it("renders a searchable timezone field that submits the selected timezone", () => {
    render(<TimezoneSelectHarness />);

    const input = screen.getByRole("combobox", { name: /timezone/i });
    const form = screen.getByTestId("timezone-form") as HTMLFormElement;
    expect(input).toHaveValue("UTC");
    expect(input).toBeRequired();
    expect(new FormData(form).get("timezone")).toBe("UTC");

    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: "Tokyo" } });
    fireEvent.keyDown(input, { key: "Enter" });

    expect(input).toHaveValue("Asia/Tokyo");
    expect(new FormData(form).get("timezone")).toBe("Asia/Tokyo");
  });

  it("forwards described-by, invalid, disabled, and class props", () => {
    render(
      <TimezoneSelect
        value="UTC"
        onChange={() => undefined}
        name="timezone"
        disabled
        className="property-timezone"
        inputClassName="property-timezone__input"
        aria-describedby="timezone-error"
        aria-invalid
      />,
    );

    const input = screen.getByRole("combobox", { name: /timezone/i });
    expect(input).toBeDisabled();
    expect(input).toHaveClass("property-timezone__input");
    expect(input.closest("label")).toHaveClass("property-timezone");
    expect(input).toHaveAttribute("aria-describedby", "timezone-error");
    expect(input).toHaveAttribute("aria-invalid", "true");
  });
});

function TimezoneSelectHarness() {
  const [value, setValue] = useState("UTC");
  return (
    <form data-testid="timezone-form">
      <TimezoneSelect value={value} onChange={setValue} name="timezone" required />
    </form>
  );
}
