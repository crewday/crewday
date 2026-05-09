import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import FormField from "./FormField";
import formsCss from "@/styles/forms.css?raw";

describe("FormField", () => {
  it("keeps requirement text in accessible labels while exposing state hooks", () => {
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
    const optionalTextarea = screen.getByLabelText("Notes Optional");

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

  it("uses design tokens for distinct requirement marker colors", () => {
    expect(formsCss).toMatch(
      /\.form-field__requirement--required\s*{\s*color: var\(--moss\);/m,
    );
    expect(formsCss).toMatch(
      /\.form-field__requirement--optional\s*{\s*color: var\(--ink-3\);/m,
    );
  });
});
