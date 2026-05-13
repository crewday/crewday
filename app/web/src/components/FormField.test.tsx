import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import FormField from "./FormField";
import formsCss from "@/styles/forms.css?raw";

describe("FormField", () => {
  it("keeps requirement text in labels while exposing state hooks", () => {
    render(
      <form>
        <FormField label="Name" requirement="required">
          <input />
        </FormField>
        <FormField label="Notes" requirement="optional">
          <textarea />
        </FormField>
      </form>,
    );

    const requiredInput = screen.getByLabelText("Name Required");
    const optionalTextarea = screen.getByLabelText(/^Notes\b/);

    expect(requiredInput.closest("label")).toHaveClass("form-field--required");
    expect(optionalTextarea.closest("label")).toHaveClass("form-field--optional");
    expect(screen.getByText("Required")).toHaveClass(
      "form-field__requirement",
      "form-field__requirement--required",
    );
    expect(screen.getByText("Optional")).toHaveClass(
      "form-field__requirement",
      "form-field__requirement--optional",
    );
    expect(screen.getByLabelText(/^Name\b/)).toBe(requiredInput);
    expect(screen.getByLabelText(/^Notes\b/)).toBe(optionalTextarea);
  });

  it("renders help text after the control while preserving described-by links", () => {
    render(
      <FormField
        label="Threshold"
        requirement="optional"
        helpId="threshold-help"
        helpText="Used to flag low stock."
      >
        <input aria-describedby="threshold-help" />
      </FormField>,
    );

    const input = screen.getByLabelText(/^Threshold\b/);
    const help = screen.getByText("Used to flag low stock.");

    expect(input).toHaveAttribute("aria-describedby", "threshold-help");
    expect(help).toHaveAttribute("id", "threshold-help");
    expect(input.compareDocumentPosition(help) & Node.DOCUMENT_POSITION_FOLLOWING).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
  });

  it("keeps optional markers intentionally hidden but easy to re-show", () => {
    expect(formsCss).toMatch(
      /\.form-field__requirement--required\s*{\s*color: var\(--moss\);/m,
    );
    expect(formsCss).toMatch(
      /\.form-field__requirement--optional\s*{\s*display: none;\s*color: var\(--ink-3\);/m,
    );
  });

  it("exposes a responsive two-column form layout with full-row help", () => {
    expect(formsCss).toContain(".form-layout--two-column");
    expect(formsCss).toMatch(
      /\.form-layout--two-column\s*{\s*grid-template-columns: repeat\(2, minmax\(0, 1fr\)\);/m,
    );
    expect(formsCss).toMatch(
      /\.form-layout__help\s*{\s*grid-column: 1 \/ -1;/m,
    );
    expect(formsCss).toMatch(
      /@media \(max-width: 480px\)\s*{[\s\S]*\.form-layout--two-column,[\s\S]*grid-template-columns: minmax\(0, 1fr\);/m,
    );
  });
});
