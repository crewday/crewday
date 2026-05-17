import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import {
  InlineNumberField,
  InlineIconField,
  InlineNoteField,
  InlineSearchableSelectField,
  InlineSelectField,
  InlineTableForm,
  type InlineTableColumn,
  type InlineTableRow,
  InlineTextField,
  InlineTimeField,
} from "./InlineTableForm";
import inlineTableCss from "@/styles/inline-table-form.css?raw";

interface Draft {
  title: string;
  owner: string;
  note: string;
}

interface NumberDraft {
  quantity: string;
}

interface IconDraft {
  iconName: string;
}

const ownerOptions = [
  { value: "maria", label: "Maria" },
  { value: "enzo", label: "Enzo" },
];

const searchableOwnerOptions = [
  { value: "maria", label: "Maria", secondaryText: "Lead" },
  { value: "enzo", label: "Enzo", secondaryText: "Backup" },
  { value: "sora", label: "Sora", secondaryText: "Float" },
];

const columns: InlineTableColumn<Draft>[] = [
  {
    key: "title",
    header: "Title",
    width: { min: 180 },
    renderRead: ({ row }) => <span>{row.draft.title}</span>,
    renderEdit: ({ row, update, disabled }) => (
      <InlineTextField
        value={row.draft.title}
        disabled={disabled}
        ariaLabel="Title"
        onChange={(title) => update({ title })}
      />
    ),
  },
  {
    key: "owner",
    header: "Owner",
    renderRead: ({ row }) => <span>{row.draft.owner}</span>,
    renderEdit: ({ row, update, disabled }) => (
      <InlineSelectField
        value={row.draft.owner}
        options={ownerOptions}
        disabled={disabled}
        ariaLabel="Owner"
        onChange={(owner) => update({ owner })}
      />
    ),
  },
];

const numberColumns: InlineTableColumn<NumberDraft>[] = [
  {
    key: "quantity",
    header: "Quantity",
    renderRead: ({ row }) => <span>{row.draft.quantity}</span>,
    renderEdit: ({ row, update, disabled }) => (
      <InlineNumberField
        value={row.draft.quantity}
        min={0}
        max={12}
        step="0.25"
        placeholder="0"
        disabled={disabled}
        ariaLabel="Quantity"
        onChange={(quantity) => update({ quantity })}
      />
    ),
  },
];

const searchableColumns: InlineTableColumn<Draft>[] = [
  {
    key: "owner",
    header: "Owner",
    renderRead: ({ row }) => <span>{row.draft.owner}</span>,
    renderEdit: ({ row, update, disabled }) => (
      <InlineSearchableSelectField
        value={row.draft.owner}
        options={searchableOwnerOptions}
        disabled={disabled}
        ariaLabel="Search owner"
        blankOption={{ label: "Unassigned", secondaryText: "None" }}
        renderOptionSecondaryText={(option) => option.secondaryText}
        onChange={(owner) => update({ owner })}
      />
    ),
  },
];

const iconColumns: InlineTableColumn<IconDraft>[] = [
  {
    key: "icon",
    header: "Icon",
    width: { px: 180 },
    renderRead: ({ row }) => <span>{row.draft.iconName || "No icon"}</span>,
    renderEdit: ({ row, update, disabled }) => (
      <InlineIconField
        label="Role icon"
        value={row.draft.iconName}
        disabled={disabled}
        onChange={(iconName) => update({ iconName })}
      />
    ),
  },
];

function renderInlineTable({
  saveMode = "explicit",
  rows = [editableRow()],
  onSave = vi.fn(),
  onCancel = vi.fn(),
  onDraftChange = vi.fn(),
}: {
  saveMode?: "explicit" | "autosave";
  rows?: InlineTableRow<Draft>[];
  onSave?: (rowId: string) => void;
  onCancel?: (rowId: string) => void;
  onDraftChange?: (rowId: string, patch: Partial<Draft>) => void;
} = {}) {
  render(
    <InlineTableForm
      ariaLabel="Inline table test"
      columns={columns}
      rows={rows}
      saveMode={saveMode}
      onDraftChange={onDraftChange}
      onSave={onSave}
      onCancel={onCancel}
      renderDetail={({ row, update, disabled }) => (
        <InlineNoteField
          value={row.draft.note}
          disabled={disabled}
          ariaLabel="Note"
          onChange={(note) => update({ note })}
        />
      )}
    />,
  );
  return { onSave, onCancel, onDraftChange };
}

describe("InlineTableForm", () => {
  it("renders number fields with numeric attributes and disabled state", () => {
    render(
      <InlineNumberField
        value="1.50"
        min={0}
        max="10"
        step="0.25"
        placeholder="0.00"
        disabled
        ariaLabel="Amount"
        onChange={vi.fn()}
      />,
    );

    const input = screen.getByLabelText("Amount");
    expect(input).toHaveAttribute("type", "text");
    expect(input).toHaveAttribute("inputmode", "decimal");
    expect(input).toHaveClass("inline-table-form__control");
    expect(input).toHaveAttribute("min", "0");
    expect(input).toHaveAttribute("max", "10");
    expect(input).toHaveAttribute("step", "0.25");
    expect(input).toHaveAttribute("placeholder", "0.00");
    expect(input).toBeDisabled();
    expect((input as HTMLInputElement).value).toBe("1.50");
  });

  it("passes number field changes through as raw strings", () => {
    const onChange = vi.fn();
    render(<InlineNumberField value="1." ariaLabel="Decimal amount" onChange={onChange} />);

    const input = screen.getByLabelText("Decimal amount");
    expect((input as HTMLInputElement).value).toBe("1.");
    fireEvent.change(input, { target: { value: "-" } });

    expect(onChange).toHaveBeenCalledWith("-");
  });

  it("renders time fields with time attributes and disabled state", () => {
    render(
      <InlineTimeField
        value="09:30"
        min="08:00"
        max="18:00"
        step={900}
        disabled
        ariaLabel="Start time"
        onChange={vi.fn()}
      />,
    );

    const input = screen.getByLabelText("Start time");
    expect(input).toHaveAttribute("type", "time");
    expect(input).toHaveClass("inline-table-form__control");
    expect(input).toHaveClass("inline-table-form__control--time");
    expect(input).toHaveAttribute("min", "08:00");
    expect(input).toHaveAttribute("max", "18:00");
    expect(input).toHaveAttribute("step", "900");
    expect(input).toBeDisabled();
    expect((input as HTMLInputElement).value).toBe("09:30");
  });

  it("updates row drafts from searchable select option selection", () => {
    const onDraftChange = vi.fn();
    render(
      <InlineTableForm
        ariaLabel="Searchable owner rows"
        columns={searchableColumns}
        rows={[editableRow()]}
        saveMode="explicit"
        onDraftChange={onDraftChange}
        onSave={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    const input = screen.getByRole("combobox", { name: /search owner/i });
    expect(input).toHaveClass("inline-table-form__control", "inline-table-form__control--searchable-select");

    fireEvent.focus(input);
    fireEvent.mouseDown(screen.getByRole("option", { name: /enzo.*backup/i }));

    expect(onDraftChange).toHaveBeenCalledWith("r-1", { owner: "enzo" });
    expect(input).toHaveValue("Enzo");
  });

  it("supports blank searchable select options inside inline rows", () => {
    const onDraftChange = vi.fn();
    render(
      <InlineTableForm
        ariaLabel="Blank searchable owner rows"
        columns={searchableColumns}
        rows={[{ ...editableRow(), draft: { ...editableRow().draft, owner: "maria" } }]}
        saveMode="explicit"
        onDraftChange={onDraftChange}
        onSave={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    const input = screen.getByRole("combobox", { name: /search owner/i });
    fireEvent.focus(input);
    fireEvent.mouseDown(screen.getByRole("option", { name: /unassigned.*none/i }));

    expect(onDraftChange).toHaveBeenCalledWith("r-1", { owner: "" });
    expect(input).toHaveValue("Unassigned");
  });

  it("keeps searchable select keyboard interaction from saving or canceling the row", () => {
    const onDraftChange = vi.fn();
    const onSave = vi.fn();
    const onCancel = vi.fn();
    render(
      <InlineTableForm
        ariaLabel="Searchable keyboard rows"
        columns={searchableColumns}
        rows={[editableRow()]}
        saveMode="explicit"
        onDraftChange={onDraftChange}
        onSave={onSave}
        onCancel={onCancel}
      />,
    );

    const input = screen.getByRole("combobox", { name: /search owner/i });
    fireEvent.focus(input);
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.keyDown(input, { key: "Escape" });

    expect(onDraftChange).toHaveBeenCalledWith("r-1", { owner: "enzo" });
    expect(onSave).not.toHaveBeenCalled();
    expect(onCancel).not.toHaveBeenCalled();
  });

  it("prevents disabled searchable select changes in inline rows", () => {
    const onDraftChange = vi.fn();
    render(
      <InlineTableForm
        ariaLabel="Disabled searchable owner rows"
        columns={searchableColumns}
        rows={[{ ...editableRow(), disabled: true }]}
        saveMode="explicit"
        onDraftChange={onDraftChange}
        onSave={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    const input = screen.getByRole("combobox", { name: /search owner/i });
    expect(input).toBeDisabled();
    expect(input.closest(".searchable-select")).toHaveClass("searchable-select--disabled");

    fireEvent.focus(input);

    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(onDraftChange).not.toHaveBeenCalled();
  });

  it("selects and clears icons through the inline icon field", async () => {
    const onChange = vi.fn();
    const { rerender } = render(<InlineIconField label="Role icon" value="" onChange={onChange} />);

    fireEvent.click(screen.getByRole("button", { name: "Role icon: No icon. Edit icon" }));
    await waitFor(() => expect(screen.getByLabelText("Search role icon choices")).toHaveFocus());
    fireEvent.change(screen.getByLabelText("Search role icon choices"), { target: { value: "waves" } });
    fireEvent.click(screen.getByRole("button", { name: "Select Waves icon" }));

    expect(onChange).toHaveBeenCalledWith("Waves");

    onChange.mockClear();
    rerender(<InlineIconField label="Role icon" value="ChefHat" onChange={onChange} />);
    fireEvent.click(screen.getByRole("button", { name: "Role icon: Chef Hat. Edit icon" }));
    fireEvent.click(within(screen.getByRole("group", { name: "Role icon choices" })).getByRole("button", { name: "No icon" }));

    expect(onChange).toHaveBeenCalledWith("");
  });

  it("keeps inline icon values accessible and disabled without exposing unknown names", () => {
    const onChange = vi.fn();
    render(<InlineIconField label="Role icon" value="LegacyRoleIcon" disabled onChange={onChange} />);

    const preview = screen.getByRole("button", { name: "Role icon: Unknown icon. Edit icon" });
    expect(preview).toBeDisabled();
    expect(screen.queryByText("LegacyRoleIcon")).not.toBeInTheDocument();

    fireEvent.click(preview);

    expect(screen.queryByRole("dialog", { name: "Role icon choices" })).not.toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("uses inline icon fields inside rows without leaking picker keys to row shortcuts", async () => {
    const onDraftChange = vi.fn();
    const onSave = vi.fn();
    const onCancel = vi.fn();
    const onDelete = vi.fn();
    render(
      <InlineTableForm
        ariaLabel="Icon rows"
        columns={iconColumns}
        rows={[{ id: "icon-1", editing: true, dirty: true, draft: { iconName: "ChefHat" }, label: "Chef" }]}
        saveMode="explicit"
        onDraftChange={onDraftChange}
        onSave={onSave}
        onCancel={onCancel}
        onDelete={onDelete}
      />,
    );

    const preview = screen.getByRole("button", { name: "Role icon: Chef Hat. Edit icon" });
    fireEvent.keyDown(preview, { key: "Enter" });
    await waitFor(() => expect(screen.getByLabelText("Search role icon choices")).toHaveFocus());

    const search = screen.getByLabelText("Search role icon choices");
    fireEvent.keyDown(search, { key: "Enter" });
    fireEvent.keyDown(search, { key: "d" });
    fireEvent.keyDown(search, { key: "Escape" });

    expect(onSave).not.toHaveBeenCalled();
    expect(onCancel).not.toHaveBeenCalled();
    expect(onDelete).not.toHaveBeenCalled();

    fireEvent.click(preview);
    fireEvent.click(within(screen.getByRole("group", { name: "Role icon choices" })).getByRole("button", { name: "No icon" }));

    expect(onDraftChange).toHaveBeenCalledWith("icon-1", { iconName: "" });
  });

  it("passes time field changes through as raw strings", () => {
    const onChange = vi.fn();
    render(<InlineTimeField value="09:30" ariaLabel="Shift start time" onChange={onChange} />);

    fireEvent.change(screen.getByLabelText("Shift start time"), { target: { value: "14:45" } });

    expect(onChange).toHaveBeenCalledWith("14:45");
  });

  it("saves and cancels table rows from number fields", () => {
    const onSave = vi.fn();
    const onCancel = vi.fn();
    render(
      <InlineTableForm
        ariaLabel="Number rows"
        columns={numberColumns}
        rows={[{ id: "n-1", editing: true, dirty: true, draft: { quantity: "1.5" } }]}
        saveMode="explicit"
        onDraftChange={vi.fn()}
        onSave={onSave}
        onCancel={onCancel}
        getRowLabel={(row) => `Quantity ${row.draft.quantity}`}
      />,
    );

    const input = screen.getByLabelText("Quantity");
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.keyDown(input, { key: "Escape" });

    expect(onSave).toHaveBeenCalledWith("n-1");
    expect(onCancel).toHaveBeenCalledWith("n-1");
  });

  it("renders column render props and multi-row detail content", () => {
    renderInlineTable();

    expect(screen.getByRole("table", { name: "Inline table test" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Title" })).toBeInTheDocument();
    expect(screen.getByLabelText("Title")).toHaveValue("Confirm linen");
    expect(screen.getByLabelText("Owner")).toHaveValue("maria");
    expect(screen.getByLabelText("Note")).toHaveValue("Bring spare keys.");
  });

  it("saves explicit rows from the save button and Enter", () => {
    const first = renderInlineTable();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(first.onSave).toHaveBeenCalledWith("r-1");

    const input = screen.getByLabelText("Title");
    fireEvent.keyDown(input, { key: "Enter" });
    expect(first.onSave).toHaveBeenCalledTimes(2);
  });

  it("uses row background for dirty state and keeps Save as the rightmost action", () => {
    renderInlineTable();

    const rowGroup = screen.getByLabelText("Confirm linen");
    const buttons = within(rowGroup).getAllByRole("button");

    expect(screen.queryByText("Dirty")).toBeNull();
    expect(screen.queryByText("Ready")).toBeNull();
    expect(rowGroup).toHaveClass("is-dirty");
    expect(buttons.map((button) => button.getAttribute("aria-label"))).toEqual(["Cancel", "Save"]);
  });

  it("defaults to compact icon actions with accessible names", () => {
    const onDelete = vi.fn();
    render(
      <InlineTableForm
        ariaLabel="Icon action table"
        columns={columns}
        rows={[editableRow()]}
        saveMode="explicit"
        onDraftChange={vi.fn()}
        onSave={vi.fn()}
        onCancel={vi.fn()}
        onDelete={onDelete}
      />,
    );

    const rowGroup = screen.getByLabelText("Confirm linen");
    const buttons = within(rowGroup).getAllByRole("button");

    expect(buttons.map((button) => button.getAttribute("aria-label"))).toEqual([
      "Delete",
      "Cancel",
      "Save",
    ]);
    expect(buttons[2]).toHaveClass("inline-table-form__icon-btn--primary");
    fireEvent.click(within(rowGroup).getByRole("button", { name: "Delete" }));
    const dialog = screen.getByRole("alertdialog", { name: "Delete this row?" });
    expect(dialog).toHaveTextContent("Confirm linen");
    expect(onDelete).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole("button", { name: "Delete row" }));
    expect(onDelete).toHaveBeenCalledWith("r-1");
  });

  it("saves textarea rows on Enter and keeps Shift+Enter for a newline", () => {
    const { onSave } = renderInlineTable();

    fireEvent.keyDown(screen.getByLabelText("Note"), { key: "Enter", shiftKey: true });
    expect(onSave).not.toHaveBeenCalled();

    fireEvent.keyDown(screen.getByLabelText("Note"), { key: "Enter" });

    expect(onSave).toHaveBeenCalledWith("r-1");
  });

  it("autosaves dirty rows when focus leaves the row", () => {
    const { onSave } = renderInlineTable({
      saveMode: "autosave",
      rows: [{ ...editableRow(), dirty: true }],
    });

    fireEvent.blur(screen.getByLabelText("Title"), { relatedTarget: document.body });

    expect(onSave).toHaveBeenCalledWith("r-1");
  });

  it("defaults to autosave when saveMode is omitted", () => {
    const onSave = vi.fn();
    render(
      <InlineTableForm
        ariaLabel="Default autosave table"
        columns={columns}
        rows={[{ ...editableRow(), dirty: true }]}
        onDraftChange={vi.fn()}
        onSave={onSave}
        onCancel={vi.fn()}
      />,
    );

    fireEvent.blur(screen.getByLabelText("Title"), { relatedTarget: document.body });

    expect(onSave).toHaveBeenCalledWith("r-1");
  });

  it("exits unchanged autosave rows on blur without saving", () => {
    const onCancel = vi.fn();
    const onSave = vi.fn();
    renderInlineTable({
      saveMode: "autosave",
      rows: [{ ...editableRow(), dirty: false, editing: true }],
      onCancel,
      onSave,
    });

    fireEvent.blur(screen.getByLabelText("Title"), { relatedTarget: document.body });

    expect(onCancel).toHaveBeenCalledWith("r-1");
    expect(onSave).not.toHaveBeenCalled();
  });

  it("does not autosave the blur caused by Escape cancel in autosave mode", () => {
    const onCancel = vi.fn();
    const onSave = vi.fn();
    renderInlineTable({
      saveMode: "autosave",
      rows: [{ ...editableRow(), dirty: true }],
      onCancel,
      onSave,
    });

    const rowGroup = screen.getByLabelText("Confirm linen");
    fireEvent.keyDown(screen.getByLabelText("Title"), { key: "Escape" });
    fireEvent.blur(rowGroup, { relatedTarget: document.body });

    expect(onCancel).toHaveBeenCalledWith("r-1");
    expect(onSave).not.toHaveBeenCalled();
  });

  it("does not autosave the blur caused by the cancel button in autosave mode", () => {
    const onCancel = vi.fn();
    const onSave = vi.fn();
    renderInlineTable({
      saveMode: "autosave",
      rows: [{ ...editableRow(), dirty: true }],
      onCancel,
      onSave,
    });

    const cancelButton = screen.getByRole("button", { name: "Cancel" });
    fireEvent.pointerDown(cancelButton);
    fireEvent.blur(screen.getByLabelText("Title"), { relatedTarget: cancelButton });
    fireEvent.click(cancelButton);

    expect(onCancel).toHaveBeenCalledWith("r-1");
    expect(onSave).not.toHaveBeenCalled();
  });

  it("cancels new or dirty edits on Escape", () => {
    const { onCancel } = renderInlineTable({
      rows: [{ ...editableRow(), isNew: true, dirty: true }],
    });

    fireEvent.keyDown(screen.getByLabelText("Title"), { key: "Escape" });

    expect(onCancel).toHaveBeenCalledWith("r-1");
  });

  it("keeps a row selected and focused after Enter save and cancel-button exits", () => {
    function Harness() {
      const [rows, setRows] = useState<InlineTableRow<Draft>[]>([
        { ...editableRow(), editing: true, dirty: true },
      ]);
      const finishEditing = (rowId: string) => {
        setRows((current) => current.map((row) => (
          row.id === rowId ? { ...row, editing: false, dirty: false } : row
        )));
      };

      return (
        <InlineTableForm
          ariaLabel="Selection after edit"
          columns={columns}
          rows={rows}
          saveMode="explicit"
          onDraftChange={vi.fn()}
          onSave={finishEditing}
          onCancel={finishEditing}
          onEdit={(rowId) => {
            setRows((current) => current.map((row) => (
              row.id === rowId ? { ...row, editing: true, dirty: true } : row
            )));
          }}
        />
      );
    }

    render(<Harness />);

    fireEvent.keyDown(screen.getByLabelText("Title"), { key: "Enter" });

    let rowGroup = screen.getByLabelText("Confirm linen");
    expect(rowGroup).toHaveClass("is-selected");
    expect(rowGroup).toHaveFocus();

    fireEvent.click(within(rowGroup).getByRole("button", { name: "Edit" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    rowGroup = screen.getByLabelText("Confirm linen");
    expect(rowGroup).toHaveClass("is-selected");
    expect(rowGroup).toHaveFocus();
  });

  it("shows validation and error states in the row detail", () => {
    renderInlineTable({
      rows: [{
        ...editableRow(),
        validation: "Title is required.",
        error: "Save failed.",
      }],
    });

    const rowGroup = screen.getByLabelText("Confirm linen");
    expect(within(rowGroup).getByText("Title is required.")).toHaveClass(
      "inline-table-form__message--validation",
    );
    expect(within(rowGroup).getByText("Save failed.")).toHaveClass(
      "inline-table-form__message--error",
    );
    expect(rowGroup).toHaveClass("has-error", "has-validation");
  });

  it("does not allow locked read rows to enter edit mode", () => {
    const onEdit = vi.fn();
    render(
      <InlineTableForm
        ariaLabel="Locked rows"
        columns={columns}
        rows={[{ ...editableRow(), editing: false, disabled: true }]}
        saveMode="explicit"
        onDraftChange={vi.fn()}
        onSave={vi.fn()}
        onCancel={vi.fn()}
        onEdit={onEdit}
      />,
    );

    const edit = screen.getByRole("button", { name: "Edit" });
    expect(edit).toBeDisabled();
    fireEvent.click(edit);
    expect(onEdit).not.toHaveBeenCalled();
  });

  it("double-clicks a read cell into edit mode and focuses that field", () => {
    function Harness() {
      const [rows, setRows] = useState<InlineTableRow<Draft>[]>([
        { ...editableRow(), editing: false, dirty: false },
      ]);
      return (
        <InlineTableForm
          ariaLabel="Editable read rows"
          columns={columns}
          rows={rows}
          saveMode="explicit"
          onDraftChange={vi.fn()}
          onSave={vi.fn()}
          onCancel={vi.fn()}
          activationMode="doubleClick"
          onEdit={(rowId) => {
            setRows((current) => current.map((row) => (
              row.id === rowId ? { ...row, editing: true } : row
            )));
          }}
        />
      );
    }

    render(<Harness />);

    fireEvent.doubleClick(screen.getByText("maria"));

    expect(screen.getByLabelText("Owner")).toHaveFocus();
  });

  it("defaults rows to read mode and focuses a cell on single click", () => {
    function Harness() {
      const [rows, setRows] = useState<InlineTableRow<Draft>[]>([
        { id: "r-1", dirty: false, draft: { title: "Confirm linen", owner: "maria", note: "" } },
      ]);
      return (
        <InlineTableForm
          ariaLabel="Single click edit rows"
          columns={columns}
          rows={rows}
          saveMode="explicit"
          onDraftChange={vi.fn()}
          onSave={vi.fn()}
          onCancel={vi.fn()}
          onEdit={(rowId) => {
            setRows((current) => current.map((row) => (
              row.id === rowId ? { ...row, editing: true } : row
            )));
          }}
        />
      );
    }

    render(<Harness />);

    fireEvent.click(screen.getByText("maria"));

    expect(screen.getByLabelText("Owner")).toHaveFocus();
  });

  it("does not enter edit mode when single-clicking interactive read content", () => {
    const onEdit = vi.fn();
    const interactiveColumns: InlineTableColumn<Draft>[] = [
      {
        ...columns[0]!,
        renderRead: ({ row }) => (
          <button type="button" onClick={() => undefined}>
            {row.draft.title}
          </button>
        ),
      },
      columns[1]!,
    ];

    render(
      <InlineTableForm
        ariaLabel="Interactive read rows"
        columns={interactiveColumns}
        rows={[{ ...editableRow(), editing: false, dirty: false }]}
        saveMode="explicit"
        onDraftChange={vi.fn()}
        onSave={vi.fn()}
        onCancel={vi.fn()}
        onEdit={onEdit}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Confirm linen" }));

    expect(onEdit).not.toHaveBeenCalled();
    expect(screen.queryByLabelText("Title")).toBeNull();
  });

  it("supports factory create rows by default and replaces them after create", () => {
    const onCreate = vi.fn();
    render(
      <InlineTableForm
        ariaLabel="Factory create rows"
        columns={columns}
        rows={[]}
        createRowLabel="New row"
        createEmptyDraft={() => ({ title: "", owner: "maria", note: "" })}
        validateCreate={(draft) => draft.title.trim() ? null : "Title is required."}
        onCreate={onCreate}
        onDraftChange={vi.fn()}
        onSave={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    const createGroup = screen.getByLabelText("New row");
    expect(createGroup).toHaveClass("inline-table-form__group--trailing-create", "is-editing");
    expect(within(createGroup).queryByRole("button", { name: "Cancel" })).toBeNull();
    fireEvent.keyDown(within(createGroup).getByLabelText("Title"), { key: "Enter" });
    expect(within(createGroup).getByText("Title is required.")).toBeInTheDocument();
    expect(onCreate).not.toHaveBeenCalled();

    fireEvent.change(within(createGroup).getByLabelText("Title"), { target: { value: "Created row" } });
    expect(within(createGroup).getByRole("button", { name: "Cancel" })).toBeInTheDocument();
    fireEvent.keyDown(within(createGroup).getByLabelText("Title"), { key: "Enter" });

    expect(onCreate).toHaveBeenCalledWith({ title: "Created row", owner: "maria", note: "" });
    expect(screen.getByLabelText("New row")).toHaveClass("is-editing");
  });

  it("renders the shared empty state by default only when no create row exists", () => {
    const { rerender } = render(
      <InlineTableForm
        ariaLabel="Empty rows"
        columns={columns}
        rows={[]}
        onDraftChange={vi.fn()}
        onSave={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    expect(screen.getByRole("heading", { name: "No rows yet" })).toBeInTheDocument();

    rerender(
      <InlineTableForm
        ariaLabel="Empty rows"
        columns={columns}
        rows={[]}
        createEmptyDraft={() => ({ title: "", owner: "maria", note: "" })}
        onCreate={vi.fn()}
        onDraftChange={vi.fn()}
        onSave={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    expect(screen.queryByRole("heading", { name: "No rows yet" })).toBeNull();
    expect(screen.getByLabelText("New row")).toBeInTheDocument();
  });

  it("uses structured column widths with flex defaults and fixed pixel columns", () => {
    const widthColumns: InlineTableColumn<Draft>[] = [
      { ...columns[0]!, width: 2 },
      { ...columns[1]!, width: { px: 96 } },
      {
        key: "note",
        header: "Note",
        width: { flex: 0.5, min: 80, max: 180 },
        renderRead: ({ row }) => <span>{row.draft.note}</span>,
        renderEdit: ({ row, update }) => (
          <InlineTextField value={row.draft.note} onChange={(note) => update({ note })} />
        ),
      },
    ];
    const { container } = render(
      <InlineTableForm
        ariaLabel="Width rows"
        columns={widthColumns}
        rows={[editableRow()]}
        onDraftChange={vi.fn()}
        onSave={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    expect(container.querySelector(".inline-table-form")).toHaveStyle({
      "--inline-table-columns": "minmax(120px, 2fr) 96px minmax(80px, 180px) minmax(112px, max-content)",
    });
  });

  it("selects a read row on single click in double-click mode and supports e/dd shortcuts", () => {
    const onEdit = vi.fn();
    const onDelete = vi.fn();
    render(
      <InlineTableForm
        ariaLabel="Shortcut rows"
        columns={columns}
        rows={[{ ...editableRow(), editing: false, dirty: false }]}
        saveMode="explicit"
        onDraftChange={vi.fn()}
        onSave={vi.fn()}
        onCancel={vi.fn()}
        onDelete={onDelete}
        onEdit={onEdit}
        activationMode="doubleClick"
      />,
    );

    fireEvent.click(screen.getByText("maria"));
    const rowGroup = screen.getByLabelText("Confirm linen");
    expect(rowGroup).toHaveClass("is-selected");
    expect(rowGroup).toHaveFocus();

    fireEvent.keyDown(rowGroup, { key: "Enter" });
    expect(onEdit).toHaveBeenCalledWith("r-1");

    fireEvent.keyDown(rowGroup, { key: "e" });
    expect(onEdit).toHaveBeenCalledTimes(2);

    fireEvent.keyDown(rowGroup, { key: "d" });
    expect(onDelete).not.toHaveBeenCalled();
    expect(rowGroup).toHaveClass("is-delete-armed");
    expect(screen.queryByRole("alertdialog", { name: "Delete this row?" })).toBeNull();
    fireEvent.keyDown(rowGroup, { key: "d" });
    expect(onDelete).toHaveBeenCalledWith("r-1");
  });

  it("selects the next row after dd delete, or the previous row when deleting the last row", () => {
    function Harness() {
      const [rows, setRows] = useState<InlineTableRow<Draft>[]>([
        { ...editableRow(), id: "r-1", editing: false, dirty: false, draft: { title: "First", owner: "maria", note: "" } },
        { ...editableRow(), id: "r-2", editing: false, dirty: false, draft: { title: "Second", owner: "enzo", note: "" } },
        { ...editableRow(), id: "r-3", editing: false, dirty: false, draft: { title: "Third", owner: "maria", note: "" } },
      ]);

      return (
        <InlineTableForm
          ariaLabel="Delete selection rows"
          columns={columns}
          rows={rows}
          saveMode="explicit"
          onDraftChange={vi.fn()}
          onSave={vi.fn()}
          onCancel={vi.fn()}
          onDelete={(rowId) => {
            setRows((current) => current.filter((row) => row.id !== rowId));
          }}
          getRowLabel={(row) => row.draft.title}
          activationMode="doubleClick"
        />
      );
    }

    render(<Harness />);

    fireEvent.click(screen.getByLabelText("Second"));
    fireEvent.keyDown(screen.getByLabelText("Second"), { key: "d" });
    fireEvent.keyDown(screen.getByLabelText("Second"), { key: "d" });
    expect(screen.getByLabelText("Third")).toHaveClass("is-selected");
    expect(screen.getByLabelText("Third")).toHaveFocus();

    fireEvent.keyDown(screen.getByLabelText("Third"), { key: "d" });
    fireEvent.keyDown(screen.getByLabelText("Third"), { key: "d" });
    expect(screen.getByLabelText("First")).toHaveClass("is-selected");
    expect(screen.getByLabelText("First")).toHaveFocus();
  });

  it("lets callers discard draft changes on Escape cancel", () => {
    function Harness() {
      const [rows, setRows] = useState<InlineTableRow<Draft>[]>([
        {
          id: "r-1",
          editing: true,
          dirty: false,
          committedDraft: { title: "Original", owner: "maria", note: "Original note" },
          draft: { title: "Original", owner: "maria", note: "Original note" },
        },
      ]);

      return (
        <InlineTableForm
          ariaLabel="Discard rows"
          columns={columns}
          rows={rows}
          saveMode="explicit"
          onDraftChange={(rowId, patch) => {
            setRows((current) => current.map((row) => (
              row.id === rowId
                ? { ...row, draft: { ...row.draft, ...patch }, dirty: true }
                : row
            )));
          }}
          onSave={vi.fn()}
          onCancel={(rowId) => {
            setRows((current) => current.map((row) => (
              row.id === rowId
                ? { ...row, draft: row.committedDraft ?? row.draft, dirty: false, editing: false }
                : row
            )));
          }}
        />
      );
    }

    render(<Harness />);

    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Changed" } });
    fireEvent.keyDown(screen.getByLabelText("Title"), { key: "Escape" });

    expect(screen.getByText("Original")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("Changed")).toBeNull();
  });

  it("moves selected rows with ArrowUp and ArrowDown, skipping disabled and editing rows", () => {
    const rows: InlineTableRow<Draft>[] = [
      { ...editableRow(), id: "r-1", editing: false, dirty: false },
      { ...editableRow(), id: "r-2", editing: false, dirty: false },
      { ...editableRow(), id: "r-3", editing: false, dirty: false, disabled: true },
      { ...editableRow(), id: "r-4", editing: true, dirty: true },
      { ...editableRow(), id: "r-5", editing: false, dirty: false },
    ];

    render(
      <InlineTableForm
        ariaLabel="Arrow navigation rows"
        columns={columns}
        rows={rows}
        saveMode="explicit"
        onDraftChange={vi.fn()}
        onSave={vi.fn()}
        onCancel={vi.fn()}
        activationMode="doubleClick"
        getRowLabel={(_, index) => `Row ${index + 1}`}
      />,
    );

    const firstRow = screen.getByLabelText("Row 1");
    const secondRow = screen.getByLabelText("Row 2");
    const fifthRow = screen.getByLabelText("Row 5");

    fireEvent.click(firstRow);
    expect(firstRow).toHaveClass("is-selected");

    fireEvent.keyDown(firstRow, { key: "ArrowDown" });
    expect(secondRow).toHaveClass("is-selected");
    expect(secondRow).toHaveFocus();

    fireEvent.keyDown(secondRow, { key: "ArrowDown" });
    expect(fifthRow).toHaveClass("is-selected");
    expect(fifthRow).toHaveFocus();

    fireEvent.keyDown(fifthRow, { key: "ArrowUp" });
    expect(secondRow).toHaveClass("is-selected");
    expect(secondRow).toHaveFocus();
  });

  it("clears the delete warning when the dd shortcut window expires", () => {
    vi.useFakeTimers();
    try {
      const onDelete = vi.fn();
      render(
        <InlineTableForm
          ariaLabel="Timed shortcut rows"
          columns={columns}
          rows={[{ ...editableRow(), editing: false, dirty: false }]}
          saveMode="explicit"
          onDraftChange={vi.fn()}
          onSave={vi.fn()}
          onCancel={vi.fn()}
          onDelete={onDelete}
          activationMode="doubleClick"
        />,
      );

      fireEvent.click(screen.getByText("maria"));
      const rowGroup = screen.getByLabelText("Confirm linen");
      fireEvent.keyDown(rowGroup, { key: "d" });
      expect(rowGroup).toHaveClass("is-delete-armed");

      act(() => {
        vi.advanceTimersByTime(651);
      });

      expect(rowGroup).not.toHaveClass("is-delete-armed");
      fireEvent.keyDown(rowGroup, { key: "d" });
      expect(onDelete).not.toHaveBeenCalled();
      expect(rowGroup).toHaveClass("is-delete-armed");
    } finally {
      vi.useRealTimers();
    }
  });

  it("focuses the first field when editing a selected row with e", () => {
    function Harness() {
      const [rows, setRows] = useState<InlineTableRow<Draft>[]>([
        { ...editableRow(), editing: false, dirty: false },
      ]);
      return (
        <InlineTableForm
          ariaLabel="Keyboard edit rows"
          columns={columns}
          rows={rows}
          saveMode="explicit"
          onDraftChange={vi.fn()}
          onSave={vi.fn()}
          onCancel={vi.fn()}
          activationMode="doubleClick"
          onEdit={(rowId) => {
            setRows((current) => current.map((row) => (
              row.id === rowId ? { ...row, editing: true } : row
            )));
          }}
        />
      );
    }

    render(<Harness />);

    fireEvent.click(screen.getByText("maria"));
    fireEvent.keyDown(screen.getByLabelText("Confirm linen"), { key: "e" });

    expect(screen.getByLabelText("Title")).toHaveFocus();
  });

  it("selects a trailing create row, edits it from Enter, and selects the created row after save", () => {
    function Harness() {
      const [rows, setRows] = useState<InlineTableRow<Draft>[]>([
        { ...editableRow(), id: "saved-1", editing: false, dirty: false },
      ]);
      const [createRow, setCreateRow] = useState<InlineTableRow<Draft>>({
        id: "create-1",
        isNew: true,
        editing: true,
        dirty: false,
        draft: { title: "", owner: "maria", note: "" },
      });

      return (
        <InlineTableForm
          ariaLabel="Rows with trailing create"
          columns={columns}
          rows={rows}
          trailingCreateRow={createRow}
          saveMode="explicit"
          onDraftChange={(rowId, patch) => {
            if (rowId === createRow.id) {
              setCreateRow((row) => ({
                ...row,
                dirty: true,
                draft: { ...row.draft, ...patch },
              }));
              return;
            }
            setRows((current) => current.map((row) => (
              row.id === rowId ? { ...row, draft: { ...row.draft, ...patch } } : row
            )));
          }}
          onSave={(rowId) => {
            if (rowId !== createRow.id) return;
            setRows((current) => [
              ...current,
              { id: "saved-2", editing: false, dirty: false, draft: createRow.draft },
            ]);
            setCreateRow({
              id: "create-2",
              isNew: true,
              editing: true,
              dirty: false,
              draft: { title: "", owner: "maria", note: "" },
            });
          }}
          onCancel={vi.fn()}
          onDelete={vi.fn()}
          getRowLabel={(row) => row.draft.title || "New row"}
          activationMode="doubleClick"
        />
      );
    }

    render(<Harness />);

    const createGroup = screen.getByLabelText("New row");

    expect(screen.getByLabelText("Confirm linen")).toBeInTheDocument();
    expect(createGroup).toHaveClass("inline-table-form__group--trailing-create");
    expect(createGroup).toHaveClass("is-editing");
    expect(within(createGroup).getByLabelText("Title")).not.toHaveFocus();
    expect(within(createGroup).queryByRole("button", { name: "Delete" })).toBeNull();
    expect(within(createGroup).queryByRole("button", { name: "Cancel" })).toBeNull();

    within(createGroup).getByLabelText("Title").focus();
    expect(createGroup).not.toHaveClass("is-selected");
    expect(within(createGroup).getByLabelText("Title")).toHaveFocus();

    const savedRow = screen.getByLabelText("Confirm linen");
    fireEvent.click(savedRow);
    fireEvent.keyDown(savedRow, { key: "ArrowDown" });
    expect(createGroup).toHaveClass("is-selected");
    expect(createGroup).toHaveFocus();

    fireEvent.keyDown(createGroup, { key: "Enter" });
    expect(screen.getByLabelText("Title")).toHaveFocus();

    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Created row" } });
    expect(within(createGroup).getByRole("button", { name: "Cancel" })).toBeInTheDocument();
    fireEvent.keyDown(screen.getByLabelText("Title"), { key: "Enter" });

    const createdRow = screen.getByLabelText("Created row");
    expect(createdRow).toHaveClass("is-selected");
    expect(createdRow).toHaveFocus();
    const nextCreateRow = screen.getByLabelText("New row");
    expect(nextCreateRow).toHaveClass("inline-table-form__group--trailing-create");
    expect(nextCreateRow).toHaveClass("is-editing");
  });

  it("keeps mobile label hooks and phone card downgrade styles in CSS", () => {
    expect(inlineTableCss).toContain(".inline-table-form__mobile-label");
    expect(inlineTableCss).toContain(".inline-table-form__group.is-delete-armed");
    expect(inlineTableCss).toContain(".inline-table-form__group:focus-visible");
    expect(inlineTableCss).toContain(".inline-table-form__group.is-selected:focus-visible");
    expect(inlineTableCss).toContain(".inline-table-form__group.is-delete-armed:focus-visible");
    expect(inlineTableCss).toContain(".inline-table-form__icon-selector .icon-selector__selected:disabled:hover");
    expect(inlineTableCss).toMatch(
      /@media \(max-width: 720px\)\s*{[\s\S]*\.inline-table-form__head\s*{\s*display: none;/m,
    );
    expect(inlineTableCss).toMatch(
      /@media \(max-width: 720px\)\s*{[\s\S]*\.inline-table-form__td\s*{[\s\S]*grid-template-columns: minmax\(90px, 0\.35fr\) minmax\(0, 1fr\);/m,
    );
  });
});

function editableRow(): InlineTableRow<Draft> {
  return {
    id: "r-1",
    editing: true,
    dirty: true,
    draft: {
      title: "Confirm linen",
      owner: "maria",
      note: "Bring spare keys.",
    },
  };
}
