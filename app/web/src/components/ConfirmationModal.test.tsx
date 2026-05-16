import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ConfirmationModal from "./ConfirmationModal";

let originalClose: typeof HTMLDialogElement.prototype.close;
let originalShowModal: typeof HTMLDialogElement.prototype.showModal;

function renderConfirmation(props?: Partial<Parameters<typeof ConfirmationModal>[0]>) {
  const onCancel = vi.fn();
  const onConfirm = vi.fn();
  render(
    <ConfirmationModal
      open
      title="Remove direct assignments?"
      confirmLabel="Remove assignments"
      onCancel={onCancel}
      onConfirm={onConfirm}
      {...props}
    >
      <p>Direct assignments will be removed.</p>
    </ConfirmationModal>,
  );
  return { onCancel, onConfirm };
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

describe("ConfirmationModal", () => {
  it("renders alertdialog semantics with destructive confirmation controls", () => {
    const { onConfirm } = renderConfirmation();
    const dialog = screen.getByRole("alertdialog", {
      name: "Remove direct assignments?",
    });

    expect(dialog).toHaveAttribute("aria-describedby");
    expect(within(dialog).getByText("Direct assignments will be removed.")).toBeInTheDocument();
    const confirm = within(dialog).getByRole("button", {
      name: "Remove assignments",
    });
    expect(confirm).toHaveClass("btn--rust");

    fireEvent.click(confirm);
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("cancels from the secondary action", () => {
    const { onCancel, onConfirm } = renderConfirmation();
    const dialog = screen.getByRole("alertdialog", {
      name: "Remove direct assignments?",
    });

    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("disables actions while pending", () => {
    const { onCancel, onConfirm } = renderConfirmation({ pending: true });
    const dialog = screen.getByRole("alertdialog", {
      name: "Remove direct assignments?",
    });

    expect(within(dialog).getByRole("button", { name: "Cancel" })).toBeDisabled();
    expect(
      within(dialog).getByRole("button", { name: "Remove assignments" }),
    ).toBeDisabled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Remove assignments" }));

    expect(onCancel).not.toHaveBeenCalled();
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("supports a moss confirmation tone", () => {
    renderConfirmation({ tone: "moss" });
    const dialog = screen.getByRole("alertdialog", {
      name: "Remove direct assignments?",
    });

    expect(
      within(dialog).getByRole("button", { name: "Remove assignments" }),
    ).toHaveClass("btn--moss");
  });
});
