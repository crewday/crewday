import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, extname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import FormModal, { FormModalField, FormModalGrid } from "./FormModal";
import formsCss from "@/styles/forms.css?raw";

const TEST_DIR = dirname(fileURLToPath(import.meta.url));
const APP_SRC_ROOT = resolve(TEST_DIR, "..");
const FORM_MODAL_SOURCE_DIRS = ["components", "pages"];
const FORM_MODAL_SHELL_ALLOWLIST = new Map<string, string>([
  ["components/FormModal.tsx", "Shared FormModal owns the structured form modal shell."],
]);
const FORBIDDEN_FORM_MODAL_SHELL_CLASSES = [
  {
    className: "llm-registry-form__close",
    reason: "Legacy admin LLM form close buttons must use FormModal.",
  },
  {
    className: "llm-registry-form__head",
    reason: "Legacy admin LLM form headers must use FormModal.",
  },
  {
    className: "llm-registry-form__body",
    reason: "Legacy admin LLM form bodies must use FormModal.",
  },
  {
    className: "llm-registry-form__footer",
    reason: "Legacy admin LLM form footers must use FormModal.",
  },
  {
    className: "inv-create__head",
    reason: "Inventory-derived form headers must come from FormModal.",
  },
  {
    className: "inv-create__close",
    reason: "Inventory-derived form close buttons must come from FormModal.",
  },
  {
    className: "inv-create__body",
    reason: "Inventory-derived form bodies must come from FormModal.",
  },
  {
    className: "inv-create__footer",
    reason: "Inventory-derived form footers must come from FormModal.",
  },
  {
    className: "sheet-form__head",
    reason: "Direct sheet form headers must use FormModal.",
  },
  {
    className: "sheet-form__close",
    reason: "Direct sheet form close buttons must use FormModal.",
  },
  {
    className: "sheet-form__body",
    reason: "Direct sheet form bodies must use FormModal.",
  },
  {
    className: "sheet-form__footer",
    reason: "Direct sheet form footers must use FormModal.",
  },
] as const;

let originalClose: typeof HTMLDialogElement.prototype.close;
let originalShowModal: typeof HTMLDialogElement.prototype.showModal;

function sourceFiles(root: string): string[] {
  const files: string[] = [];
  const visit = (dir: string) => {
    for (const entry of readdirSync(dir)) {
      const path = join(dir, entry);
      const stat = statSync(path);
      if (stat.isDirectory()) {
        visit(path);
        continue;
      }
      const ext = extname(path);
      if (
        (ext === ".ts" || ext === ".tsx")
        && !path.endsWith(".test.ts")
        && !path.endsWith(".test.tsx")
        && !path.endsWith(".spec.ts")
        && !path.endsWith(".spec.tsx")
        && !path.endsWith(".d.ts")
      ) {
        files.push(path);
      }
    }
  };
  visit(root);
  return files;
}

function relativeSourcePath(file: string): string {
  return relative(APP_SRC_ROOT, file).split(sep).join("/");
}

function lineNumber(source: string, index: number): number {
  return source.slice(0, index).split("\n").length;
}

function escapedRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function adHocFormModalShellViolations(): string[] {
  return FORM_MODAL_SOURCE_DIRS.flatMap((dir) => {
    return sourceFiles(resolve(APP_SRC_ROOT, dir)).flatMap((file) => {
      const relativePath = relativeSourcePath(file);
      const allowlistReason = FORM_MODAL_SHELL_ALLOWLIST.get(relativePath);
      if (allowlistReason != null) return [];

      const source = readFileSync(file, "utf8");
      return FORBIDDEN_FORM_MODAL_SHELL_CLASSES.flatMap(({ className, reason }) => {
        const classPattern = new RegExp(`\\b${escapedRegExp(className)}\\b`, "g");
        return Array.from(source.matchAll(classPattern), (match) => {
          return `${relativePath}:${lineNumber(source, match.index)} uses .${className}: ${reason}`;
        });
      });
    });
  });
}

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
  it("keeps structured form modal shells centralized in the shared component", () => {
    const violations = adHocFormModalShellViolations();
    const allowlist = Array.from(
      FORM_MODAL_SHELL_ALLOWLIST,
      ([file, reason]) => `${file}: ${reason}`,
    );

    expect(
      violations,
      [
        "Structured form modal shells in app/web/src/pages and app/web/src/components must use FormModal.",
        "This guard only blocks legacy shell classes, so confirmation dialogs, menus, and media editors stay out of scope.",
        "Allowed shell owners:",
        ...allowlist,
      ].join("\n"),
    ).toEqual([]);
  });

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

  it("falls back to the open attribute when native dialog methods throw", () => {
    HTMLDialogElement.prototype.showModal = function showModal() {
      throw new Error("dialog not available");
    };
    HTMLDialogElement.prototype.close = function close() {
      throw new Error("dialog not available");
    };
    const { rerender } = render(
      <FormModal open title="Create item" onClose={vi.fn()}>
        <FormModalField label="Name" requirement="required">
          <input required />
        </FormModalField>
      </FormModal>,
    );

    const dialog = screen.getByRole("dialog", { name: "Create item" });
    expect(dialog).toHaveAttribute("open");

    rerender(
      <FormModal open={false} title="Create item" onClose={vi.fn()}>
        <FormModalField label="Name" requirement="required">
          <input required />
        </FormModalField>
      </FormModal>,
    );

    expect(dialog).not.toHaveAttribute("open");
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

  it("supports actionless section content without rendering an empty footer", () => {
    render(
      <FormModal
        open
        title="Assignment chain"
        titleId="assignment-chain-title"
        eyebrow="Assignment chain"
        contentElement="section"
        onClose={vi.fn()}
      >
        <p>Fallback rungs</p>
      </FormModal>,
    );

    const dialog = screen.getByRole("dialog", { name: "Assignment chain" });
    expect(dialog.querySelector("section.form-modal")).toBeInTheDocument();
    expect(dialog.querySelector("form")).not.toBeInTheDocument();
    expect(dialog.querySelector(".form-modal__footer")).not.toBeInTheDocument();
  });
});
