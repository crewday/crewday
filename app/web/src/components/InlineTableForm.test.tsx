import { QueryClient, QueryClientProvider, useInfiniteQuery } from "@tanstack/react-query";
import type { InfiniteData } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import {
  InlineDateField,
  InlineNumberField,
  InlineIconField,
  InlineTableLoadMore,
  InlineNoteField,
  InlineSearchableSelectField,
  InlineSelectField,
  InlineTagPickerField,
  InlineTableForm,
  type InlineTableColumn,
  type InlineTableRow,
  InlineTextField,
  InlineTimeField,
  inlineTableNextCursor,
  useInlineTableInfiniteRows,
} from "./InlineTableForm";
import type { ListEnvelope } from "@/lib/listResponse";
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

interface RoleDraft {
  roles: string[];
}

interface CursorRecord {
  id: string;
  title: string;
  owner: string;
  note: string;
}

type CursorPage = ListEnvelope<CursorRecord>;

const ownerOptions = [
  { value: "maria", label: "Maria" },
  { value: "enzo", label: "Enzo" },
];

const searchableOwnerOptions = [
  { value: "maria", label: "Maria", secondaryText: "Lead" },
  { value: "enzo", label: "Enzo", secondaryText: "Backup" },
  { value: "sora", label: "Sora", secondaryText: "Float" },
];

const roleOptions = [
  { value: "manager", label: "Manager" },
  { value: "employee", label: "Employee" },
  { value: "admin", label: "Admin" },
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

const tagColumns: InlineTableColumn<RoleDraft>[] = [
  {
    key: "roles",
    header: "Roles",
    renderRead: ({ row }) => <span>{row.draft.roles.join(", ")}</span>,
    renderEdit: ({ row, update, disabled }) => (
      <InlineTagPickerField
        value={row.draft.roles}
        options={roleOptions}
        disabled={disabled}
        ariaLabel="Document roles"
        onChange={(roles) => update({ roles })}
      />
    ),
  },
];

function cssDeclarationValue(css: string, selector: string, property: string) {
  const escapedSelector = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const rule = css.match(new RegExp(`${escapedSelector}\\s*\\{(?<body>[^}]*)\\}`));
  const value = rule?.groups?.body.match(new RegExp(`${property}\\s*:\\s*(?<value>[^;]+);`));
  return value?.groups?.value.trim();
}

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

function SearchableInlineTable() {
  const allRows = [
    rowWithTitle("r-1", "Confirm linen"),
    rowWithTitle("r-2", "Restock coffee"),
  ];
  const [query, setQuery] = useState("linen");
  const visibleRows = allRows.filter((row) => (
    row.draft.title.toLowerCase().includes(query.trim().toLowerCase())
  ));

  return (
    <InlineTableForm
      ariaLabel="Searchable rows"
      columns={columns}
      rows={visibleRows}
      search={{
        value: query,
        onChange: setQuery,
        label: "Search rows",
        placeholder: "Search titles",
        clearLabel: "Clear row search",
        resultSummary: `${visibleRows.length} of ${allRows.length} rows`,
        filters: <button type="button">Open only</button>,
      }}
      onDraftChange={vi.fn()}
      onSave={vi.fn()}
      onCancel={vi.fn()}
    />
  );
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

  it("passes accessibility error state through text and note fields", () => {
    render(
      <>
        <InlineTextField
          value="Night Manager"
          ariaLabel="Role key"
          ariaInvalid
          ariaDescribedBy="role-key-error"
          onChange={vi.fn()}
        />
        <InlineNoteField
          value="Too long"
          ariaLabel="Role description"
          ariaInvalid
          ariaDescribedBy="role-description-error"
          onChange={vi.fn()}
        />
      </>,
    );

    expect(screen.getByLabelText("Role key")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByLabelText("Role key")).toHaveAttribute("aria-describedby", "role-key-error");
    expect(screen.getByLabelText("Role description")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByLabelText("Role description")).toHaveAttribute(
      "aria-describedby",
      "role-description-error",
    );
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

  it("keeps date fields wide enough for ISO date values", () => {
    const onChange = vi.fn();
    render(
      <InlineDateField
        value="2026-05-29"
        disabled
        ariaLabel="Closure date"
        onChange={onChange}
      />,
    );

    const input = screen.getByLabelText("Closure date");
    expect(input).toHaveAttribute("type", "date");
    expect(input).toHaveClass("inline-table-form__control", "inline-table-form__control--date");
    expect(input).toHaveValue("2026-05-29");
    expect(input).toBeDisabled();
    expect(cssDeclarationValue(
      inlineTableCss,
      ".inline-table-form__control--date",
      "min-width",
    )).toBe("min(136px, 100%)");
    expect(inlineTableCss).toMatch(
      /@media\s*\(max-width:\s*720px\)[\s\S]*\.inline-table-form__td\s*\{[\s\S]*grid-template-columns:\s*minmax\(90px,\s*0\.35fr\)\s*minmax\(0,\s*1fr\);/,
    );
  });

  it("toggles fixed tag options and normalizes duplicate unknown values into option order", () => {
    const onChange = vi.fn();
    render(
      <InlineTagPickerField
        value={["admin", "manager", "manager", "legacy"]}
        options={[roleOptions[0]!, roleOptions[0]!, roleOptions[1]!, roleOptions[2]!]}
        ariaLabel="Agent doc roles"
        onChange={onChange}
      />,
    );

    const group = screen.getByRole("group", { name: "Agent doc roles" });
    const buttons = within(group).getAllByRole("button");
    expect(buttons.map((button) => button.textContent)).toEqual(["Manager", "Employee", "Admin"]);
    expect(within(group).getByRole("button", { name: "Manager" })).toHaveAttribute("aria-pressed", "true");
    expect(within(group).getByRole("button", { name: "Admin" })).toHaveAttribute("aria-pressed", "true");

    fireEvent.click(within(group).getByRole("button", { name: "Employee" }));
    expect(onChange).toHaveBeenLastCalledWith(["manager", "employee", "admin"]);

    fireEvent.click(within(group).getByRole("button", { name: "Admin" }));
    expect(onChange).toHaveBeenLastCalledWith(["manager"]);
  });

  it("keeps tag picker row shortcuts scoped without blocking Escape cancel", () => {
    const onDraftChange = vi.fn();
    const onSave = vi.fn();
    const onCancel = vi.fn();
    const onDelete = vi.fn();
    render(
      <InlineTableForm
        ariaLabel="Role rows"
        columns={tagColumns}
        rows={[
          {
            id: "role-1",
            editing: true,
            dirty: true,
            draft: { roles: ["employee"] },
            label: "Agent roles",
          },
        ]}
        saveMode="explicit"
        onDraftChange={onDraftChange}
        onSave={onSave}
        onCancel={onCancel}
        onDelete={onDelete}
      />,
    );

    const rowGroup = screen.getByLabelText("Agent roles");
    const manager = within(rowGroup).getByRole("button", { name: "Manager" });
    fireEvent.keyDown(manager, { key: "Enter" });
    fireEvent.keyDown(manager, { key: "d" });

    expect(onDraftChange).toHaveBeenCalledWith("role-1", { roles: ["manager", "employee"] });
    expect(onSave).not.toHaveBeenCalled();
    expect(onDelete).not.toHaveBeenCalled();

    fireEvent.keyDown(manager, { key: "Escape" });

    expect(onCancel).toHaveBeenCalledWith("role-1");
  });

  it("supports tag picker labels and keyboard toggling", () => {
    const onChange = vi.fn();
    render(
      <InlineTagPickerField
        value={["employee"]}
        options={roleOptions}
        label="Document roles"
        onChange={onChange}
      />,
    );

    const group = screen.getByRole("group", { name: "Document roles" });
    fireEvent.keyDown(within(group).getByRole("button", { name: "Manager" }), { key: "Enter" });
    expect(onChange).toHaveBeenLastCalledWith(["manager", "employee"]);

    fireEvent.keyDown(within(group).getByRole("button", { name: "Employee" }), { key: " " });
    expect(onChange).toHaveBeenLastCalledWith([]);
  });

  it("prevents disabled tag picker changes", () => {
    const onChange = vi.fn();
    const options = [
      roleOptions[0]!,
      { ...roleOptions[1]!, disabled: true },
      roleOptions[2]!,
    ];
    const { rerender } = render(
      <InlineTagPickerField
        value={[]}
        options={options}
        ariaLabel="Editable roles"
        onChange={onChange}
      />,
    );

    const disabledOption = screen.getByRole("button", { name: "Employee" });
    expect(disabledOption).toBeDisabled();
    fireEvent.click(disabledOption);
    fireEvent.keyDown(disabledOption, { key: "Enter" });
    expect(onChange).not.toHaveBeenCalled();

    rerender(
      <InlineTagPickerField
        value={["manager"]}
        options={roleOptions}
        ariaLabel="Editable roles"
        disabled
        onChange={onChange}
      />,
    );

    expect(screen.getByRole("group", { name: "Editable roles" })).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByRole("button", { name: "Manager" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Admin" }));

    expect(onChange).not.toHaveBeenCalled();
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

    expect(screen.queryByText("No icon")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Role icon: No icon. Edit icon" }));
    await waitFor(() => expect(screen.getByLabelText("Search role icon choices")).toHaveFocus());
    fireEvent.change(screen.getByLabelText("Search role icon choices"), { target: { value: "waves" } });
    fireEvent.click(screen.getByRole("button", { name: "Select Waves icon" }));

    expect(onChange).toHaveBeenCalledWith("Waves");

    onChange.mockClear();
    rerender(<InlineIconField label="Role icon" value="ChefHat" onChange={onChange} />);
    expect(screen.queryByText("Chef Hat")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Role icon: Chef Hat. Edit icon" }));
    expect(screen.getByText("Chef Hat")).toBeInTheDocument();
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

  it("lets closed inline icon selector Escape cancel the row", () => {
    const onCancel = vi.fn();
    render(
      <InlineTableForm
        ariaLabel="Closed icon escape rows"
        columns={iconColumns}
        rows={[{ id: "icon-1", editing: true, dirty: true, draft: { iconName: "ChefHat" }, label: "Chef" }]}
        saveMode="explicit"
        onDraftChange={vi.fn()}
        onSave={vi.fn()}
        onCancel={onCancel}
      />,
    );

    fireEvent.keyDown(screen.getByRole("button", { name: "Role icon: Chef Hat. Edit icon" }), { key: "Escape" });

    expect(onCancel).toHaveBeenCalledWith("icon-1");
  });

  it("closes an open inline icon selector before Escape cancels the row", async () => {
    const onCancel = vi.fn();
    render(
      <InlineTableForm
        ariaLabel="Open icon escape rows"
        columns={iconColumns}
        rows={[{ id: "icon-1", editing: true, dirty: true, draft: { iconName: "ChefHat" }, label: "Chef" }]}
        saveMode="explicit"
        onDraftChange={vi.fn()}
        onSave={vi.fn()}
        onCancel={onCancel}
      />,
    );

    const preview = screen.getByRole("button", { name: "Role icon: Chef Hat. Edit icon" });
    fireEvent.keyDown(preview, { key: "Enter" });
    await waitFor(() => expect(screen.getByLabelText("Search role icon choices")).toHaveFocus());

    fireEvent.keyDown(screen.getByLabelText("Search role icon choices"), { key: "Escape" });

    expect(screen.queryByRole("dialog", { name: "Role icon choices" })).not.toBeInTheDocument();
    expect(onCancel).not.toHaveBeenCalled();

    fireEvent.keyDown(preview, { key: "Escape" });

    expect(onCancel).toHaveBeenCalledWith("icon-1");
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

  it("lets callers provide delete confirmation copy", () => {
    const onDelete = vi.fn();
    render(
      <InlineTableForm
        ariaLabel="Custom delete table"
        columns={columns}
        rows={[{ ...editableRow(), editing: false, dirty: false }]}
        saveMode="explicit"
        onDraftChange={vi.fn()}
        onSave={vi.fn()}
        onCancel={vi.fn()}
        onDelete={onDelete}
        renderDeleteConfirmation={({ label }) => ({
          title: "Delete area?",
          confirmLabel: "Delete area",
          children: (
            <p>
              Delete <strong>{label}</strong>? This will also delete 2 child areas.
            </p>
          ),
        })}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    const dialog = screen.getByRole("alertdialog", { name: "Delete area?" });

    expect(dialog).toHaveTextContent("This will also delete 2 child areas.");
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete area" }));
    expect(onDelete).toHaveBeenCalledWith("r-1");
  });

  it("routes keyboard delete through caller-provided confirmation copy", () => {
    const onDelete = vi.fn();
    render(
      <InlineTableForm
        ariaLabel="Keyboard custom delete table"
        columns={columns}
        rows={[{ ...editableRow(), editing: false, dirty: false }]}
        saveMode="explicit"
        onDraftChange={vi.fn()}
        onSave={vi.fn()}
        onCancel={vi.fn()}
        onDelete={onDelete}
        onEdit={vi.fn()}
        activationMode="doubleClick"
        renderDeleteConfirmation={({ label }) => ({
          title: "Delete area?",
          confirmLabel: "Delete area",
          children: <p>Delete {label}? This will also delete child areas.</p>,
        })}
      />,
    );

    fireEvent.click(screen.getByText("maria"));
    const rowGroup = screen.getByLabelText("Confirm linen");
    fireEvent.keyDown(rowGroup, { key: "d" });
    fireEvent.keyDown(rowGroup, { key: "d" });

    const dialog = screen.getByRole("alertdialog", { name: "Delete area?" });
    expect(dialog).toHaveTextContent("This will also delete child areas.");
    expect(onDelete).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete area" }));
    expect(onDelete).toHaveBeenCalledWith("r-1");
  });

  it("does not render reorder controls or draggable rows by default", () => {
    render(
      <InlineTableForm
        ariaLabel="Default rows"
        columns={columns}
        rows={[
          { ...editableRow(), editing: false, dirty: false },
          rowWithTitle("r-2", "Restock towels"),
        ]}
        saveMode="explicit"
        onDraftChange={vi.fn()}
        onSave={vi.fn()}
        onCancel={vi.fn()}
        onDelete={vi.fn()}
        onEdit={vi.fn()}
      />,
    );

    const rowGroup = screen.getByLabelText("Confirm linen");

    expect(rowGroup).not.toHaveAttribute("draggable");
    expect(within(rowGroup).getAllByRole("button").map((button) => button.getAttribute("aria-label"))).toEqual([
      "Delete",
      "Edit",
    ]);
    expect(screen.queryByRole("button", { name: /move .* up/i })).toBeNull();
  });

  it("reports pointer drag reorders with movement and ordered ids", () => {
    const onReorder = vi.fn();
    const dataTransfer = {
      effectAllowed: "",
      dropEffect: "",
      setData: vi.fn(),
      getData: vi.fn(),
    };
    render(
      <InlineTableForm
        ariaLabel="Draggable rows"
        columns={columns}
        rows={[
          rowWithTitle("r-1", "First"),
          rowWithTitle("r-2", "Second"),
          rowWithTitle("r-3", "Third"),
        ]}
        saveMode="explicit"
        onDraftChange={vi.fn()}
        onSave={vi.fn()}
        onCancel={vi.fn()}
        onReorder={onReorder}
        getRowLabel={(row) => row.draft.title}
      />,
    );

    const first = screen.getByLabelText("First");
    const third = screen.getByLabelText("Third");

    expect(first).toHaveAttribute("draggable", "true");
    fireEvent.dragStart(first, { dataTransfer });
    fireEvent.dragOver(third, { dataTransfer, clientY: 20 });
    fireEvent.drop(third, { dataTransfer, clientY: 20 });

    expect(onReorder).toHaveBeenCalledWith({
      rowId: "r-1",
      fromIndex: 0,
      toIndex: 2,
      orderedRowIds: ["r-2", "r-3", "r-1"],
      orderedRows: [
        expect.objectContaining({ id: "r-2" }),
        expect.objectContaining({ id: "r-3" }),
        expect.objectContaining({ id: "r-1" }),
      ],
    });
  });

  it("moves rows with keyboard controls and disables edge moves", () => {
    const onReorder = vi.fn();
    const onEdit = vi.fn();
    render(
      <InlineTableForm
        ariaLabel="Keyboard reorder rows"
        columns={columns}
        rows={[
          rowWithTitle("r-1", "First"),
          rowWithTitle("r-2", "Second"),
          rowWithTitle("r-3", "Third"),
        ]}
        saveMode="explicit"
        onDraftChange={vi.fn()}
        onSave={vi.fn()}
        onCancel={vi.fn()}
        onEdit={onEdit}
        onReorder={onReorder}
        getRowLabel={(row) => row.draft.title}
      />,
    );

    expect(screen.getByRole("button", { name: "Move First up" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Move Third down" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Move First down" }));

    expect(onReorder).toHaveBeenCalledWith(expect.objectContaining({
      rowId: "r-1",
      fromIndex: 0,
      toIndex: 1,
      orderedRowIds: ["r-2", "r-1", "r-3"],
    }));

    const moveFirstDown = screen.getByRole("button", { name: "Move First down" });
    moveFirstDown.focus();
    fireEvent.keyDown(moveFirstDown, { key: "Enter" });

    expect(onEdit).not.toHaveBeenCalled();
  });

  it("omits reorder controls for disabled edge rows and locks adjacent moves", () => {
    render(
      <InlineTableForm
        ariaLabel="Locked edge reorder rows"
        columns={columns}
        rows={[
          { ...rowWithTitle("r-0", "Locked top"), disabled: true },
          rowWithTitle("r-1", "First movable"),
          rowWithTitle("r-2", "Second movable"),
          { ...rowWithTitle("r-3", "Locked bottom"), disabled: true },
        ]}
        saveMode="explicit"
        onDraftChange={vi.fn()}
        onSave={vi.fn()}
        onCancel={vi.fn()}
        onReorder={vi.fn()}
        getRowLabel={(row) => row.draft.title}
      />,
    );

    expect(screen.queryByRole("button", { name: "Move Locked top down" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Move Locked bottom up" })).toBeNull();
    expect(screen.getByRole("button", { name: "Move First movable up" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Move Second movable down" })).toBeDisabled();
  });

  it("keeps edit, save, and delete shortcuts working when reorder is enabled", () => {
    const onDelete = vi.fn();
    const onEdit = vi.fn();
    const onSave = vi.fn();
    render(
      <InlineTableForm
        ariaLabel="Reorder shortcut rows"
        columns={columns}
        rows={[
          rowWithTitle("r-1", "Read row"),
          { ...rowWithTitle("r-2", "Editing row"), editing: true, dirty: true },
        ]}
        saveMode="explicit"
        onDraftChange={vi.fn()}
        onSave={onSave}
        onCancel={vi.fn()}
        onDelete={onDelete}
        onEdit={onEdit}
        onReorder={vi.fn()}
        activationMode="doubleClick"
        getRowLabel={(row) => row.draft.title}
      />,
    );

    const readRow = screen.getByLabelText("Read row");
    fireEvent.click(readRow);
    fireEvent.keyDown(readRow, { key: "e" });
    fireEvent.keyDown(readRow, { key: "d" });
    fireEvent.keyDown(readRow, { key: "d" });
    fireEvent.keyDown(screen.getByDisplayValue("Editing row"), { key: "Enter" });

    expect(onEdit).toHaveBeenCalledWith("r-1");
    expect(onDelete).toHaveBeenCalledWith("r-1");
    expect(onSave).toHaveBeenCalledWith("r-2");
    expect(screen.queryByRole("button", { name: "Move Editing row up" })).toBeNull();
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

  it("saves a dirty autosave row before switching rows from an Edit action", async () => {
    const onSave = vi.fn();
    const onEdit = vi.fn();

    function Harness() {
      const [rows, setRows] = useState<InlineTableRow<Draft>[]>([
        { ...rowWithTitle("r-1", "Confirm linen"), editing: true, dirty: true },
        rowWithTitle("r-2", "Restock towels"),
      ]);

      return (
        <InlineTableForm
          ariaLabel="Autosave switch rows"
          columns={columns}
          rows={rows}
          saveMode="autosave"
          onDraftChange={vi.fn()}
          onSave={(rowId) => {
            onSave(rowId);
            setRows((current) => current.map((row) => (
              row.id === rowId ? { ...row, editing: false, dirty: false } : row
            )));
          }}
          onCancel={vi.fn()}
          onEdit={(rowId) => {
            onEdit(rowId);
            setRows((current) => current.map((row) => (
              row.id === rowId ? { ...row, editing: true } : row
            )));
          }}
        />
      );
    }

    render(<Harness />);

    const title = screen.getByLabelText("Title");
    const edit = within(screen.getByLabelText("Restock towels")).getByRole("button", { name: "Edit" });
    fireEvent.pointerDown(edit);
    fireEvent.blur(title, { relatedTarget: edit });
    fireEvent.click(edit);

    expect(onSave).toHaveBeenCalledWith("r-1");
    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onEdit).toHaveBeenCalledWith("r-2");
    expect(onSave.mock.invocationCallOrder[0]).toBeLessThan(onEdit.mock.invocationCallOrder[0]!);
    await waitFor(() => expect(screen.getByLabelText("Confirm linen")).toHaveClass("is-reading"));
    expect(screen.getByLabelText("Restock towels")).toHaveClass("is-editing");
  });

  it("saves a dirty autosave row before keyboard editing a selected target row", () => {
    const onSave = vi.fn();
    const onEdit = vi.fn();

    function Harness() {
      const [rows, setRows] = useState<InlineTableRow<Draft>[]>([
        { ...rowWithTitle("r-1", "Confirm linen"), editing: true, dirty: true },
        rowWithTitle("r-2", "Restock towels"),
      ]);

      return (
        <InlineTableForm
          ariaLabel="Keyboard autosave switch rows"
          columns={columns}
          rows={rows}
          saveMode="autosave"
          activationMode="doubleClick"
          onDraftChange={vi.fn()}
          onSave={(rowId) => {
            onSave(rowId);
            setRows((current) => current.map((row) => (
              row.id === rowId ? { ...row, editing: false, dirty: false } : row
            )));
          }}
          onCancel={vi.fn()}
          onEdit={(rowId) => {
            onEdit(rowId);
            setRows((current) => current.map((row) => (
              row.id === rowId ? { ...row, editing: true } : row
            )));
          }}
        />
      );
    }

    render(<Harness />);

    const targetRow = screen.getByLabelText("Restock towels");
    fireEvent.click(targetRow);
    expect(onSave).not.toHaveBeenCalled();

    fireEvent.keyDown(targetRow, { key: "Enter" });

    expect(onSave).toHaveBeenCalledWith("r-1");
    expect(onEdit).toHaveBeenCalledWith("r-2");
    expect(onSave.mock.invocationCallOrder[0]).toBeLessThan(onEdit.mock.invocationCallOrder[0]!);
    expect(screen.getByLabelText("Restock towels")).toHaveClass("is-editing");
  });

  it("saves before cell activation and focuses the intended target field", async () => {
    const onSave = vi.fn();
    const onEdit = vi.fn();

    function Harness() {
      const [rows, setRows] = useState<InlineTableRow<Draft>[]>([
        { ...rowWithTitle("r-1", "Confirm linen"), editing: true, dirty: true },
        { ...rowWithTitle("r-2", "Restock towels"), draft: { title: "Restock towels", owner: "enzo", note: "" } },
      ]);

      return (
        <InlineTableForm
          ariaLabel="Autosave focus switch rows"
          columns={columns}
          rows={rows}
          saveMode="autosave"
          onDraftChange={vi.fn()}
          onSave={(rowId) => {
            onSave(rowId);
            setRows((current) => current.map((row) => (
              row.id === rowId ? { ...row, editing: false, dirty: false } : row
            )));
          }}
          onCancel={vi.fn()}
          onEdit={(rowId) => {
            onEdit(rowId);
            setRows((current) => current.map((row) => (
              row.id === rowId ? { ...row, editing: true } : row
            )));
          }}
        />
      );
    }

    render(<Harness />);

    const title = screen.getByLabelText("Title");
    const targetCell = screen.getByText("enzo");
    title.focus();
    fireEvent.pointerDown(targetCell);
    fireEvent.blur(title, { relatedTarget: null });
    fireEvent.click(targetCell);

    expect(onSave).toHaveBeenCalledWith("r-1");
    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onEdit).toHaveBeenCalledWith("r-2");
    expect(onSave.mock.invocationCallOrder[0]).toBeLessThan(onEdit.mock.invocationCallOrder[0]!);
    await waitFor(() => expect(screen.getByLabelText("Owner")).toHaveFocus());
  });

  it("cancels an unchanged autosave row once before activating a different cell", async () => {
    const onCancel = vi.fn();
    const onEdit = vi.fn();

    function Harness() {
      const [rows, setRows] = useState<InlineTableRow<Draft>[]>([
        { ...rowWithTitle("r-1", "Confirm linen"), editing: true, dirty: false },
        { ...rowWithTitle("r-2", "Restock towels"), draft: { title: "Restock towels", owner: "enzo", note: "" } },
      ]);

      return (
        <InlineTableForm
          ariaLabel="Autosave unchanged focus switch rows"
          columns={columns}
          rows={rows}
          saveMode="autosave"
          onDraftChange={vi.fn()}
          onSave={vi.fn()}
          onCancel={(rowId) => {
            onCancel(rowId);
            setRows((current) => current.map((row) => (
              row.id === rowId ? { ...row, editing: false } : row
            )));
          }}
          onEdit={(rowId) => {
            onEdit(rowId);
            setRows((current) => current.map((row) => (
              row.id === rowId ? { ...row, editing: true } : row
            )));
          }}
        />
      );
    }

    render(<Harness />);

    const title = screen.getByLabelText("Title");
    const targetCell = screen.getByText("enzo");
    title.focus();
    fireEvent.pointerDown(targetCell);
    fireEvent.blur(title, { relatedTarget: null });
    fireEvent.click(targetCell);

    expect(onCancel).toHaveBeenCalledWith("r-1");
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onEdit).toHaveBeenCalledWith("r-2");
    expect(onCancel.mock.invocationCallOrder[0]).toBeLessThan(onEdit.mock.invocationCallOrder[0]!);
    await waitFor(() => expect(screen.getByLabelText("Owner")).toHaveFocus());
  });

  it("autosaves a dirty row when an outside click moves focus out of the form", () => {
    const onSave = vi.fn();
    render(
      <>
        <InlineTableForm
          ariaLabel="Outside autosave rows"
          columns={columns}
          rows={[{ ...editableRow(), dirty: true }]}
          saveMode="autosave"
          onDraftChange={vi.fn()}
          onSave={onSave}
          onCancel={vi.fn()}
        />
        <button type="button">Outside action</button>
      </>,
    );

    const title = screen.getByLabelText("Title");
    const outside = screen.getByRole("button", { name: "Outside action" });
    title.focus();
    outside.focus();
    fireEvent.blur(title, { relatedTarget: outside });
    fireEvent.click(outside);

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

  it("supports batch actions for multiple dirty rows without row save and cancel controls", () => {
    const onSubmit = vi.fn();
    const onDelete = vi.fn();
    const rows: InlineTableRow<Draft>[] = [
      {
        ...editableRow(),
        id: "r-1",
        dirty: true,
        draft: { title: "Count towels", owner: "maria", note: "" },
      },
      {
        ...editableRow(),
        id: "r-2",
        dirty: true,
        draft: { title: "Count soap", owner: "enzo", note: "" },
      },
      {
        ...editableRow(),
        id: "r-3",
        dirty: false,
        draft: { title: "Count tea", owner: "maria", note: "" },
      },
    ];

    render(
      <InlineTableForm
        ariaLabel="Batch rows"
        columns={columns}
        rows={rows}
        saveMode="batch"
        onDraftChange={vi.fn()}
        onDelete={onDelete}
        renderBatchActions={(batch) => (
          <button type="button" disabled={!batch.canSubmit} onClick={() => onSubmit(batch.dirtyRows)}>
            Commit {batch.dirtyRows.length} changes
          </button>
        )}
      />,
    );

    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
    expect(screen.getAllByRole("button", { name: "Delete" })).toHaveLength(3);

    fireEvent.click(screen.getByRole("button", { name: "Commit 2 changes" }));

    expect(onSubmit).toHaveBeenCalledWith([rows[0], rows[1]]);
  });

  it("exposes batch dirty, validation, error, saving, and disabled row state", () => {
    render(
      <InlineTableForm
        ariaLabel="Batch state rows"
        columns={columns}
        rows={[
          { ...editableRow(), id: "dirty", dirty: true },
          { ...editableRow(), id: "invalid", validation: "Title is required." },
          { ...editableRow(), id: "error", error: "Save failed." },
          { ...editableRow(), id: "saving", saving: true },
          { ...editableRow(), id: "disabled", disabled: true },
        ]}
        saveMode="batch"
        onDraftChange={vi.fn()}
        renderBatchActions={(batch) => (
          <output>
            dirty {batch.dirtyRows.length}, validation {batch.validationRows.length}, errors{" "}
            {batch.errorRows.length}, saving {batch.savingRows.length}, disabled{" "}
            {batch.disabledRows.length}, can submit {String(batch.canSubmit)}
          </output>
        )}
      />,
    );

    expect(screen.getByText("Title is required.")).toBeInTheDocument();
    expect(screen.getByText("Save failed.")).toBeInTheDocument();
    expect(screen.getByText("Saving")).toBeInTheDocument();
    expect(screen.getByText("Locked")).toBeInTheDocument();
    expect(screen.getByText("dirty 5, validation 1, errors 1, saving 1, disabled 1, can submit false"))
      .toBeInTheDocument();
  });

  it("lets callers discard all batch draft changes when a batch cancel is provided", () => {
    function Harness() {
      const [rows, setRows] = useState<InlineTableRow<Draft>[]>([
        {
          id: "r-1",
          editing: true,
          dirty: true,
          committedDraft: { title: "Original", owner: "maria", note: "" },
          draft: { title: "Changed", owner: "maria", note: "" },
        },
      ]);

      return (
        <InlineTableForm
          ariaLabel="Batch discard rows"
          columns={columns}
          rows={rows}
          saveMode="batch"
          onDraftChange={(rowId, patch) => {
            setRows((current) => current.map((row) => (
              row.id === rowId
                ? { ...row, draft: { ...row.draft, ...patch }, dirty: true }
                : row
            )));
          }}
          onBatchCancel={() => {
            setRows((current) => current.map((row) => ({
              ...row,
              draft: row.committedDraft ?? row.draft,
              dirty: false,
            })));
          }}
          renderBatchActions={(batch) => (
            <button type="button" disabled={!batch.canDiscard} onClick={batch.discard}>
              Discard changes
            </button>
          )}
        />
      );
    }

    render(<Harness />);

    expect(screen.getByDisplayValue("Changed")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Discard changes" }));

    expect(screen.getByDisplayValue("Original")).toBeInTheDocument();
  });

  it("does not row-save or row-cancel from edited-cell keyboard shortcuts in batch mode", () => {
    const onSave = vi.fn();
    const onCancel = vi.fn();
    const onDraftChange = vi.fn();
    render(
      <InlineTableForm
        ariaLabel="Batch keyboard rows"
        columns={columns}
        rows={[editableRow()]}
        saveMode="batch"
        onDraftChange={onDraftChange}
        onSave={onSave}
        onCancel={onCancel}
        renderBatchActions={() => <button type="button">Commit changes</button>}
      />,
    );

    const title = screen.getByLabelText("Title");
    fireEvent.change(title, { target: { value: "Batch count" } });
    expect(fireEvent.keyDown(title, { key: "Enter" })).toBe(false);
    fireEvent.keyDown(title, { key: "Escape" });

    expect(onDraftChange).toHaveBeenCalledWith("r-1", { title: "Batch count" });
    expect(onSave).not.toHaveBeenCalled();
    expect(onCancel).not.toHaveBeenCalled();
  });

  it("preserves caller-owned multi-row editing when switching rows in batch mode", () => {
    const onSave = vi.fn();
    const onCancel = vi.fn();
    const onEdit = vi.fn();

    function Harness() {
      const [rows, setRows] = useState<InlineTableRow<Draft>[]>([
        { ...rowWithTitle("r-1", "Count towels"), editing: true, dirty: true },
        { ...rowWithTitle("r-2", "Count soap"), editing: true, dirty: true },
        rowWithTitle("r-3", "Count tea"),
      ]);

      return (
        <InlineTableForm
          ariaLabel="Batch switch rows"
          columns={columns}
          rows={rows}
          saveMode="batch"
          onDraftChange={vi.fn()}
          onSave={onSave}
          onCancel={onCancel}
          onEdit={(rowId) => {
            onEdit(rowId);
            setRows((current) => current.map((row) => (
              row.id === rowId ? { ...row, editing: true } : row
            )));
          }}
          renderBatchActions={() => <button type="button">Commit changes</button>}
        />
      );
    }

    render(<Harness />);

    fireEvent.click(within(screen.getByLabelText("Count tea")).getByRole("button", { name: "Edit" }));

    expect(onSave).not.toHaveBeenCalled();
    expect(onCancel).not.toHaveBeenCalled();
    expect(onEdit).toHaveBeenCalledWith("r-3");
    expect(screen.getByLabelText("Count towels")).toHaveClass("is-editing");
    expect(screen.getByLabelText("Count soap")).toHaveClass("is-editing");
    expect(screen.getByLabelText("Count tea")).toHaveClass("is-editing");
  });

  it("blocks batch submit while a dirty row is disabled", () => {
    render(
      <InlineTableForm
        ariaLabel="Batch disabled dirty rows"
        columns={columns}
        rows={[
          { ...editableRow(), id: "ready", dirty: true },
          { ...editableRow(), id: "locked", dirty: true, disabled: true },
        ]}
        saveMode="batch"
        onDraftChange={vi.fn()}
        renderBatchActions={(batch) => (
          <button type="button" disabled={!batch.canSubmit}>
            Commit {batch.dirtyRows.length} changes
          </button>
        )}
      />,
    );

    expect(screen.getByRole("button", { name: "Commit 2 changes" })).toBeDisabled();
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

  it("keeps externally controlled create rows cancellable before edits", () => {
    const onCancel = vi.fn();
    render(
      <InlineTableForm
        ariaLabel="External create rows"
        columns={columns}
        rows={[]}
        saveMode="explicit"
        trailingCreateRow={{
          id: "external-create",
          label: "New row",
          isNew: true,
          editing: true,
          dirty: false,
          draft: { title: "", owner: "maria", note: "" },
        }}
        onDraftChange={vi.fn()}
        onSave={vi.fn()}
        onCancel={onCancel}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onCancel).toHaveBeenCalledWith("external-create");
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

  it("does not render search chrome unless search props are provided", () => {
    renderInlineTable();

    expect(screen.queryByRole("search")).toBeNull();
    expect(screen.queryByLabelText("Search rows")).toBeNull();
  });

  it("supports controlled search changes, clear, summaries, and filter actions", () => {
    render(<SearchableInlineTable />);

    const toolbar = screen.getByRole("search", { name: "Searchable rows search and filters" });
    const input = within(toolbar).getByLabelText("Search rows");
    expect(input).toHaveAttribute("placeholder", "Search titles");
    expect((input as HTMLInputElement).value).toBe("linen");
    expect(within(toolbar).getByText("1 of 2 rows")).toBeInTheDocument();
    expect(screen.getByText("Confirm linen")).toBeInTheDocument();
    expect(screen.queryByText("Restock coffee")).toBeNull();
    expect(within(toolbar).getByRole("button", { name: "Open only" })).toBeInTheDocument();

    fireEvent.change(input, { target: { value: "coffee" } });
    expect((input as HTMLInputElement).value).toBe("coffee");
    expect(within(toolbar).getByText("1 of 2 rows")).toBeInTheDocument();
    expect(screen.getByText("Restock coffee")).toBeInTheDocument();
    expect(screen.queryByText("Confirm linen")).toBeNull();

    const clearButton = within(toolbar).getByRole("button", { name: "Clear row search" });
    clearButton.focus();
    fireEvent.click(clearButton);
    expect((input as HTMLInputElement).value).toBe("");
    expect(input).toHaveFocus();
    expect(within(toolbar).getByText("2 of 2 rows")).toBeInTheDocument();
    expect(screen.getByText("Confirm linen")).toBeInTheDocument();
    expect(screen.getByText("Restock coffee")).toBeInTheDocument();
  });

  it("renders a search-specific no-results state", () => {
    render(
      <InlineTableForm
        ariaLabel="Filtered empty rows"
        columns={columns}
        rows={[]}
        search={{
          value: "missing",
          onChange: vi.fn(),
          label: "Search rows",
          resultSummary: 0,
        }}
        onDraftChange={vi.fn()}
        onSave={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    expect(screen.getByRole("heading", { name: "No matching rows" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("0");
    expect(screen.queryByRole("heading", { name: "No rows yet" })).toBeNull();
  });

  it("renders empty content in a full-width table row slot", () => {
    const { container } = render(
      <InlineTableForm
        ariaLabel="Empty rows"
        columns={columns}
        rows={[]}
        emptyState="No scheduled rows."
        onDraftChange={vi.fn()}
        onSave={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    const body = container.querySelector(".inline-table-form__body");
    const emptyRow = container.querySelector(".inline-table-form__empty");
    const emptyCell = container.querySelector(".inline-table-form__empty-cell");

    expect(emptyRow).not.toBeNull();
    expect(emptyRow?.parentElement).toBe(body);
    expect(emptyRow).toHaveAttribute("role", "row");
    expect(emptyCell).toHaveAttribute("role", "cell");
    expect(emptyCell).toHaveAttribute("aria-colspan", String(columns.length + 1));
    expect(emptyCell).toHaveTextContent("No scheduled rows.");
  });

  it("keeps empty rows centered and spanning the shared grid", () => {
    expect(cssDeclarationValue(
      inlineTableCss,
      ".inline-table-form__empty",
      "box-sizing",
    )).toBe("border-box");
    expect(cssDeclarationValue(
      inlineTableCss,
      ".inline-table-form__empty",
      "justify-content",
    )).toBe("center");
    expect(cssDeclarationValue(
      inlineTableCss,
      ".inline-table-form__empty",
      "width",
    )).toBe("100%");
    expect(cssDeclarationValue(
      inlineTableCss,
      ".inline-table-form__empty",
      "text-align",
    )).toBe("center");
    expect(cssDeclarationValue(
      inlineTableCss,
      ".inline-table-form__empty-cell",
      "justify-content",
    )).toBe("center");
    expect(cssDeclarationValue(
      inlineTableCss,
      ".inline-table-form__empty-cell",
      "width",
    )).toBe("100%");
    expect(cssDeclarationValue(
      inlineTableCss,
      ".inline-table-form__empty-cell",
      "min-width",
    )).toBe("0");
    expect(inlineTableCss).toMatch(
      /@supports\s*\(grid-template-columns:\s*subgrid\)[\s\S]*\.inline-table-form__empty\s*\{[\s\S]*grid-column:\s*1\s*\/\s*-1;[\s\S]*min-width:\s*0;/,
    );
    expect(inlineTableCss).toMatch(
      /@media\s*\(max-width:\s*720px\)[\s\S]*\.inline-table-form__empty\s*\{[\s\S]*min-width:\s*0;/,
    );
  });

  it("uses caller-owned filtered empty states and custom summaries", () => {
    render(
      <InlineTableForm
        ariaLabel="Filtered action rows"
        columns={columns}
        rows={[]}
        search={{
          value: "",
          onChange: vi.fn(),
          label: "Search rows",
          resultSummary: <strong>0 matching open rows</strong>,
          isFiltered: true,
          noResultsState: <p>No open rows match filters.</p>,
        }}
        onDraftChange={vi.fn()}
        onSave={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent("0 matching open rows");
    expect(screen.getByText("No open rows match filters.")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "No rows yet" })).toBeNull();
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

  it("cancels rows entered into edit mode from keyboard Enter", async () => {
    const onCancel = vi.fn();
    function Harness() {
      const [rows, setRows] = useState<InlineTableRow<Draft>[]>([
        { ...editableRow(), editing: false, dirty: false },
      ]);
      return (
        <InlineTableForm
          ariaLabel="Keyboard cancel rows"
          columns={columns}
          rows={rows}
          saveMode="explicit"
          onDraftChange={vi.fn()}
          onSave={vi.fn()}
          onCancel={(rowId) => {
            onCancel(rowId);
            setRows((current) => current.map((row) => (
              row.id === rowId ? { ...row, editing: false, dirty: false } : row
            )));
          }}
          activationMode="doubleClick"
          onEdit={(rowId) => {
            setRows((current) => current.map((row) => (
              row.id === rowId ? { ...row, editing: true, dirty: true } : row
            )));
          }}
        />
      );
    }

    render(<Harness />);

    fireEvent.click(screen.getByText("maria"));
    fireEvent.keyDown(screen.getByLabelText("Confirm linen"), { key: "Enter" });
    await waitFor(() => expect(screen.getByLabelText("Title")).toHaveFocus());

    fireEvent.keyDown(screen.getByLabelText("Title"), { key: "Escape" });

    expect(onCancel).toHaveBeenCalledWith("r-1");
    expect(screen.getByLabelText("Confirm linen")).toHaveClass("is-reading");
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
    expect(within(createGroup).getByRole("button", { name: "Cancel" })).toBeInTheDocument();

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

  it("shows the inline table loading row while the first cursor page loads", async () => {
    const firstPage = deferred<CursorPage>();
    const fetchPage = vi.fn(async () => firstPage.promise);
    renderInfiniteInlineTable(fetchPage);

    expect(screen.getByRole("status")).toHaveTextContent("Loading rows");
    expect(screen.getByText("0 loaded")).toBeInTheDocument();

    await act(async () => {
      firstPage.resolve(cursorPage([cursorRecord("c-1", "Confirm linen")], null));
    });

    expect(await screen.findByText("Confirm linen")).toBeInTheDocument();
    expect(screen.getByText("All rows loaded")).toBeInTheDocument();
  });

  it("appends a cursor page without resetting edited draft state", async () => {
    const fetchPage = vi.fn(async (cursor: string | null) => (
      cursor === "cursor-2"
        ? cursorPage([cursorRecord("c-2", "Restock coffee")], null)
        : cursorPage([cursorRecord("c-1", "Confirm linen")], "cursor-2")
    ));
    renderInfiniteInlineTable(fetchPage);

    expect(await screen.findByText("Confirm linen")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Edited linen" } });

    fireEvent.click(screen.getByRole("button", { name: "Load more rows" }));

    expect(await screen.findByText("Restock coffee")).toBeInTheDocument();
    expect(screen.getByLabelText("Title")).toHaveValue("Edited linen");
    expect(screen.getByLabelText("Edited linen")).toHaveClass("is-editing");
    expect(fetchPage).toHaveBeenCalledWith("cursor-2");
  });

  it("renders cursor load errors and retries the failed page", async () => {
    const fetchPage = vi
      .fn<(cursor: string | null) => Promise<CursorPage>>()
      .mockResolvedValueOnce(cursorPage([cursorRecord("c-1", "Confirm linen")], "cursor-2"))
      .mockRejectedValueOnce(new Error("network"))
      .mockResolvedValueOnce(cursorPage([cursorRecord("c-2", "Restock coffee")], null));
    renderInfiniteInlineTable(fetchPage);

    expect(await screen.findByText("Confirm linen")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Load more rows" }));

    expect(await screen.findByText("Could not load the next page.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry loading rows" }));

    expect(await screen.findByText("Restock coffee")).toBeInTheDocument();
    expect(screen.queryByText("Could not load the next page.")).not.toBeInTheDocument();
    expect(fetchPage).toHaveBeenCalledTimes(3);
  });

  it("keeps cursor retry disabled while the failed page is loading again", async () => {
    const retryPage = deferred<CursorPage>();
    const fetchPage = vi
      .fn<(cursor: string | null) => Promise<CursorPage>>()
      .mockResolvedValueOnce(cursorPage([cursorRecord("c-1", "Confirm linen")], "cursor-2"))
      .mockRejectedValueOnce(new Error("network"))
      .mockImplementationOnce(async () => retryPage.promise);
    renderInfiniteInlineTable(fetchPage);

    expect(await screen.findByText("Confirm linen")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Load more rows" }));

    expect(await screen.findByText("Could not load the next page.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry loading rows" }));

    const retryButton = await screen.findByRole("button", { name: "Loading rows" });
    expect(retryButton).toBeDisabled();

    await act(async () => {
      retryPage.resolve(cursorPage([cursorRecord("c-2", "Restock coffee")], null));
    });

    expect(await screen.findByText("Restock coffee")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Loading rows" })).not.toBeInTheDocument();
  });

  it("shows the all-loaded cursor state when the final page has no next cursor", async () => {
    const fetchPage = vi.fn(async () => cursorPage([cursorRecord("c-1", "Confirm linen")], null));
    renderInfiniteInlineTable(fetchPage);

    expect(await screen.findByText("Confirm linen")).toBeInTheDocument();
    expect(screen.getByText("All rows loaded")).toBeInTheDocument();
    expect(screen.getByText("1 loaded")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load more rows" })).not.toBeInTheDocument();
  });

  it("does not expose load more when has_more is true without a next cursor", async () => {
    const fetchPage = vi.fn(async () => ({
      data: [cursorRecord("c-1", "Confirm linen")],
      next_cursor: null,
      has_more: true,
    }));
    renderInfiniteInlineTable(fetchPage);

    expect(await screen.findByText("Confirm linen")).toBeInTheDocument();
    expect(screen.getByText("All rows loaded")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load more rows" })).not.toBeInTheDocument();
  });

  it("keeps mobile label hooks and phone card downgrade styles in CSS", () => {
    expect(inlineTableCss).toContain(".inline-table-form__mobile-label");
    expect(inlineTableCss).toContain(".inline-table-form__group.is-delete-armed");
    expect(inlineTableCss).toContain(".inline-table-form__load-more");
    expect(inlineTableCss).toContain(".inline-table-form__group:focus-visible");
    expect(inlineTableCss).toContain(".inline-table-form__group.is-selected:focus-visible");
    expect(inlineTableCss).toContain(".inline-table-form__group.is-delete-armed:focus-visible");
    expect(inlineTableCss).toContain(".inline-table-form__icon-selector .icon-selector__selected:disabled:hover");
    expect(inlineTableCss).toMatch(
      /\.inline-table-form__group\.is-editing \.inline-table-form__td:not\(\.inline-table-form__td--actions\) > :not\(\.inline-table-form__mobile-label\)\s*{[\s\S]*flex: 1 1 auto;[\s\S]*min-width: 0;/m,
    );
    expect(inlineTableCss).toMatch(
      /\.inline-table-form__tag-options\s*{[\s\S]*flex-wrap: wrap;/m,
    );
    expect(inlineTableCss).toMatch(
      /\.inline-table-form__tag-option\s*{[\s\S]*min-width: 0;[\s\S]*max-width: 100%;/m,
    );
    expect(inlineTableCss).toMatch(
      /\.inline-table-form__control,\s*\.inline-table-form__note\s*{[\s\S]*border: 1px solid var\(--line\);/m,
    );
    expect(inlineTableCss).toMatch(
      /\.inline-table-form__control:hover,\s*\.inline-table-form__note:hover\s*{[\s\S]*border-color: var\(--line-strong\);/m,
    );
    expect(inlineTableCss).toMatch(
      /\.inline-table-form__control:focus,\s*\.inline-table-form__note:focus\s*{[\s\S]*border-color: var\(--moss\);/m,
    );
    expect(inlineTableCss).toMatch(
      /\.inline-table-form__control:disabled,[\s\S]*\.inline-table-form__note\[readonly\]\s*{[\s\S]*background: var\(--paper-2\);/m,
    );
    expect(inlineTableCss).toMatch(
      /\.inline-table-form__control:disabled,\s*\.inline-table-form__control\[readonly\]\s*{[\s\S]*cursor: not-allowed;/m,
    );
    expect(inlineTableCss).toMatch(
      /\.inline-table-form__tag-option:disabled,\s*\.inline-table-form__tag-option:disabled:hover\s*{[\s\S]*border-color: var\(--line\);/m,
    );
    expect(inlineTableCss).toMatch(
      /\.inline-table-form__icon-selector \.icon-selector__selected\s*{[\s\S]*border-color: var\(--line\);/m,
    );
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

function rowWithTitle(id: string, title: string): InlineTableRow<Draft> {
  return {
    id,
    editing: false,
    dirty: false,
    draft: {
      title,
      owner: "maria",
      note: "",
    },
  };
}

function renderInfiniteInlineTable(fetchPage: (cursor: string | null) => Promise<CursorPage>) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <InfiniteInlineTableHarness fetchPage={fetchPage} />
    </QueryClientProvider>,
  );
}

function InfiniteInlineTableHarness({
  fetchPage,
}: {
  fetchPage: (cursor: string | null) => Promise<CursorPage>;
}) {
  const query = useInfiniteQuery<CursorPage, Error, InfiniteData<CursorPage>, readonly ["inline-table-cursor"], string | null>({
    queryKey: ["inline-table-cursor"],
    initialPageParam: null,
    queryFn: ({ pageParam }) => fetchPage(pageParam),
    getNextPageParam: inlineTableNextCursor,
  });
  const infiniteRows = useInlineTableInfiniteRows<CursorRecord, Draft>({
    data: query.data,
    getRowId: (record) => record.id,
    mapRow: cursorRecordToRow,
  });
  const loadError = query.error
    ? query.data ? "Could not load the next page." : "Could not load rows."
    : null;

  return (
    <InlineTableForm
      ariaLabel="Cursor inline table"
      columns={columns}
      rows={infiniteRows.rows}
      saveMode="explicit"
      onDraftChange={infiniteRows.patchRowDraft}
      onEdit={(rowId) => infiniteRows.updateRow(rowId, (row) => ({ ...row, editing: true }))}
      onCancel={infiniteRows.resetRow}
      onSave={(rowId) => infiniteRows.updateRow(rowId, (row) => ({
        ...row,
        dirty: false,
        editing: false,
        committedDraft: row.draft,
      }))}
      getRowLabel={(row) => row.draft.title}
      loadMore={(
        <InlineTableLoadMore
          hasMore={query.hasNextPage}
          isInitialLoading={query.isLoading}
          isFetchingMore={query.isFetchingNextPage}
          error={loadError}
          loadedCount={infiniteRows.loadedRowCount}
          onLoadMore={() => {
            void query.fetchNextPage();
          }}
          onRetry={() => {
            if (query.data) {
              void query.fetchNextPage();
              return;
            }
            void query.refetch();
          }}
        />
      )}
    />
  );
}

function cursorRecord(id: string, title: string): CursorRecord {
  return {
    id,
    title,
    owner: "maria",
    note: "",
  };
}

function cursorPage(data: CursorRecord[], nextCursor: string | null): CursorPage {
  return {
    data,
    next_cursor: nextCursor,
    has_more: nextCursor !== null,
  };
}

function cursorRecordToRow(record: CursorRecord): InlineTableRow<Draft> {
  const draft = {
    title: record.title,
    owner: record.owner,
    note: record.note,
  };
  return {
    id: record.id,
    editing: false,
    dirty: false,
    draft,
    committedDraft: draft,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}
