import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import FormModal, { FormModalField, FormModalGrid } from "./FormModal";
import formsCss from "@/styles/forms.css?raw";

let originalClose: typeof HTMLDialogElement.prototype.close;
let originalShowModal: typeof HTMLDialogElement.prototype.showModal;

function renderModal(onClose = vi.fn()) {
  render(
    <FormModal
      open
      title="Create item"
      titleId="create-item-title"
      eyebrow="New inventory item"
      subtitle="Add the stock record before setting reorder rules."
      width="narrow"
      className="inventory-reference"
      formClassName="inventory-reference__form"
      onClose={onClose}
      actions={
        <>
          <button type="button" className="btn btn--ghost">
            Cancel
          </button>
          <button type="submit" className="btn btn--moss">
            Create item
          </button>
        </>
      }
    >
      <FormModalField label="Name" requirement="required">
        <input required />
      </FormModalField>
      <FormModalGrid data-testid="modal-grid">
        <FormModalField label="SKU" requirement="optional">
          <input />
        </FormModalField>
        <FormModalField label="Unit" requirement="required">
          <input required />
        </FormModalField>
      </FormModalGrid>
    </FormModal>,
  );
  return onClose;
}

beforeEach(() => {
  originalClose = HTMLDialogElement.prototype.close;
  originalShowModal = HTMLDialogElement.prototype.showModal;
  HTMLDialogElement.prototype.showModal = function showModal() {
    this.open = true;
  };
  HTMLDialogElement.prototype.close = function close() {
    this.open = false;
  };
});

afterEach(() => {
  cleanup();
  HTMLDialogElement.prototype.close = originalClose;
  HTMLDialogElement.prototype.showModal = originalShowModal;
});

describe("FormModal", () => {
  it("renders the shared structured modal contract", () => {
    renderModal();

    const dialog = screen.getByRole("dialog", { name: "Create item" });
    expect(dialog).toHaveClass(
      "modal",
      "modal--sheet",
      "form-modal-dialog",
      "form-modal-dialog--narrow",
      "inventory-reference",
    );
    expect(within(dialog).getByText("New inventory item")).toHaveClass(
      "form-modal__eyebrow",
    );
    expect(within(dialog).getByRole("heading", { name: "Create item" })).toHaveClass(
      "form-modal__title",
    );
    expect(within(dialog).getByText("Add the stock record before setting reorder rules.")).toHaveClass(
      "form-modal__sub",
    );
    expect(dialog.querySelector("form")).toHaveClass(
      "form-modal",
      "inventory-reference__form",
    );
    expect(dialog.querySelector(".form-modal__body")).toBeInTheDocument();
    expect(screen.getByTestId("modal-grid")).toHaveClass("form-modal__grid");
  });

  it("calls onClose from the shared close button", () => {
    const onClose = renderModal();

    fireEvent.click(screen.getByRole("button", { name: "Close" }));

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("labels the dialog and fields accessibly", () => {
    renderModal();

    const dialog = screen.getByRole("dialog", { name: "Create item" });
    expect(dialog).toHaveAttribute("aria-labelledby", "create-item-title");
    expect(within(dialog).getByLabelText(/^Name\b/)).toBeRequired();
    expect(within(dialog).getByLabelText(/^SKU\b/)).not.toBeRequired();
    expect(within(dialog).getAllByText("Required")).toHaveLength(2);
    expect(within(dialog).getByText("Optional")).toHaveClass(
      "form-field__requirement",
      "form-field__requirement--optional",
    );
  });

  it("keeps footer actions in the canonical trailing layout", () => {
    renderModal();

    const footer = screen
      .getByRole("button", { name: "Create item" })
      .closest(".form-modal__footer");

    expect(footer).toBeInTheDocument();
    expect(within(footer as HTMLElement).getAllByRole("button")).toHaveLength(2);
    expect(within(footer as HTMLElement).getAllByRole("button").map((button) => button.textContent)).toEqual([
      "Cancel",
      "Create item",
    ]);
    expect(formsCss).toMatch(
      /\.form-modal__footer,\n\.sheet-form__footer \{[\s\S]*justify-content: flex-end;/m,
    );
  });
});
